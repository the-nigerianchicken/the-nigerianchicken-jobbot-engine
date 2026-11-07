"""Reading one source: what's new, what changed, what closed, and what no one collects.

A listing no one's kinds of role keep is recorded as skipped and goes no further. A GitHub-list row's short
link is first followed to the job's own link, and its job is dated by the earliest day a list gives. A row
whose job jobbot already reads on its own board is left out, since the native copy has the full description.
A new or changed listing gets its details, within the run's budget, and is stored and queued. A list row's
details are its page, read once every row is taken; one not read in time is described later (run.py). A
posting closes only after it's missing from two successful checks in a row, and a board that suddenly answers
with nothing while it had 5 or more open counts as a failure rather than closing everything.

A board that only answers searches (Workday) is also searched for what its people want besides
internships (rules.searches), in the countries their Places choose; anyone with no place chosen has it
searched everywhere.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from jobbot_engine.model import Listing, Posting, listing_hash, posting_id, stamp
from jobbot_engine.places import countries_in, expand, keep_countries, names_a_country
from jobbot_engine.rules import searches, skip_reason
from jobbot_engine.sources import READERS, SourceError, to_posting
from jobbot_engine.sources.base import SEARCHES, Searches, Wants, from_listing
from jobbot_engine.sources.describe import Page, Read, describe_all, with_page
from jobbot_engine.sources.detect import native_id
from jobbot_engine.sources.github_list import is_short, job_link, twin_key
from jobbot_engine.sources.http import HostResting, NotThere, OutOfTime, Refused, TurnedAway, Waits
from jobbot_engine.store.db import KnownRow, SkippedRow, Store
from jobbot_engine.store.rows import ListRow, SourceState

log = logging.getLogger(__name__)

TIME_LIMIT = 90  # seconds of wall-clock time per source
# Kinds whose detail costs a request; the others carry everything in their listing.
_DETAIL_REQUESTS = {"greenhouse", "workday", "ashby"}
# New or changed list postings whose pages are read as they're found; a new list's thousands wait for later.
DESCRIBE_NOW = 40
# Seconds a source spends reading its new list postings' pages, so what the boards found is sent soon.
DESCRIBE_FOR = 15
# Short links whose job link is asked in a run, a fraction of a second each; a row whose link isn't known yet
# waits for a later run, which asks the next ones.
FOLLOW_NOW = 200
# Short links that may fail in a run before the rest wait for the next: the host is likely down.
FOLLOW_FAILURES = 3
_TWIN_WINDOW = timedelta(days=30)
_BUSY = 5
_EMPTY = "answered with no openings, though it had"
# The jobs a person's Kind of job skipped that a source keeps as examples, for the person to see.
EXAMPLES = 20


@dataclass
class Skips:
    """How many of a source's jobs one person's Kind of job skipped, and the first few (company, title,
    why)."""

    count: int = 0
    examples: list[tuple[str, str, str]] = field(default_factory=lambda: [])


@dataclass
class SourceResult:
    source_id: str
    postings_new: list[Posting] = field(default_factory=lambda: [])
    postings_changed: list[Posting] = field(default_factory=lambda: [])
    closed: list[str] = field(default_factory=lambda: [])
    skipped: int = 0
    # Each person's skips here, whether or not someone else collected the job.
    skipped_for: dict[str, Skips] = field(default_factory=lambda: {})
    error: str | None = None
    partial: bool = False
    # Why it wasn't read this run and waits, unchecked, for the next: the run's reading time ran out, or the
    # board's host turned the run away. None when it was read.
    later: str | None = None


def read_source(
    source: Mapping[str, Any],
    users: Sequence[Mapping[str, Any]],
    store: Store,
    http: Any,
    now: datetime,
    details_budget: int = 25,
    start_by: float | None = None,
    stop_by: float | None = None,
    describe_now: int = DESCRIBE_NOW,
) -> SourceResult:
    """`start_by` and `stop_by` are the run's reading time (time.monotonic()): a source not started by the
    first waits for the next run, and one still being read at the second stops where it is."""
    result = SourceResult(source["id"])
    if start_by is not None and time.monotonic() > start_by:
        result.later = "this run's reading time ran out"
        return result
    try:
        reader, wants = READERS[source["kind"]], wants_of(users, turn=f"{source['id']} {now.isoformat()}")
        if wants.queries and isinstance(reader, Searches):
            listings = reader.search(source["org"], source["company"], http, wants)
        else:
            listings = reader.openings(source["org"], source["company"], http)
    except Waits as error:
        result.later = error.why
        return result
    except TurnedAway:  # asking us to slow down: not a failure of the board's
        result.later = HostResting.why
        return result
    except SourceError as error:
        return _failed(store, result, str(error), now)
    except Exception as error:  # a reader's bug or a board's odd answer: this source fails, the run goes on
        return _failed(store, result, f"jobbot couldn't read it: {type(error).__name__}: {error}", now)
    if stop_by is not None and time.monotonic() > stop_by:  # its answer came too late to take any of it
        result.later = OutOfTime.why
        return result
    # A window of a board (the newest pages, or what changed lately) can't say what's gone.
    whole = getattr(listings, "complete", True)
    state = store.source_state(source["id"])
    emptied = False
    if whole and not listings and state is not None and state.open_count >= _BUSY:
        if not _empty_for_a_while(state):
            return _failed(store, result, f"{_EMPTY} {state.open_count} open", now)
        emptied = True  # the third empty answer in a row: the board really has emptied
    reading = _Reading(source, users, store, http, now, details_budget, result, stop_by, describe_now)
    taken = reading.take(listings)
    if not reading.resting and reading.details_tried and reading.details_failed == reading.details_tried:
        count = reading.details_tried
        noun = "posting's details" if count == 1 else "postings' details"
        message = f"none of its {count} new {noun} could be read: {reading.last_error}"
        return _failed(store, result, message, now)
    store.seen(reading.unchanged, now)
    open_count = reading.close_missing(by_absence=whole and taken, at_once=emptied)
    store.record_check(source["id"], True, None, now, found=len(result.postings_new), open_count=open_count)
    return result


def wants_of(users: Sequence[Mapping[str, Any]], turn: str | None = None) -> Wants:
    """What these people want a board searched for besides internships, and in which countries. A board is
    searched for the first SEARCHES words at most, so with more, a run's turn (its board and time) shuffles
    them: each run takes a different few, and none is left out for good."""
    queries: list[str] = []
    countries: set[str] = set()
    anywhere = False
    for person in users:
        words = searches(person["settings"])
        if not words:
            continue
        queries += words
        places = person["settings"]["rules"]["places"]
        chosen = expand(places["chosen"]) if places["on"] else set[str]()
        anywhere = anywhere or not chosen
        countries |= chosen
    unique = tuple(dict.fromkeys(queries))
    if turn is not None and len(unique) > SEARCHES:
        unique = tuple(random.Random(turn).sample(unique, len(unique)))
    return Wants(unique, None if anywhere or not unique else frozenset(countries))


def _empty_for_a_while(state: SourceState) -> bool:
    """Two empty answers already, in a row: a third means the board has emptied, not glitched."""
    return state.consecutive_failures >= 2 and (state.last_error or "").startswith(_EMPTY)


def _failed(store: Store, result: SourceResult, error: str, now: datetime) -> SourceResult:
    store.record_check(result.source_id, False, error[:500], now, found=0, open_count=0)
    result.error = error[:500]
    return result


class _Reading:
    def __init__(
        self,
        source: Mapping[str, Any],
        users: Sequence[Mapping[str, Any]],
        store: Store,
        http: Any,
        now: datetime,
        budget: int,
        result: SourceResult,
        stop_by: float | None = None,
        describe_now: int = DESCRIBE_NOW,
    ) -> None:
        self.source, self.users, self.store, self.http, self.now = source, users, store, http, now
        self.budget, self.result, self.stop_by, self.describe_now = budget, result, stop_by, describe_now
        # The board's host asked us to slow down, or the run's time ran out: the rest waits for the next run.
        self.resting = False
        self.seen_ids: set[str] = set()
        self.unchanged: list[str] = []
        self.skipped: list[SkippedRow] = []
        self.list_rows: list[ListRow] = []
        # New or changed list rows, whose pages are read once every row is taken.
        self.to_describe: list[tuple[Listing, KnownRow | None, str]] = []
        listed = source["kind"] == "github_list"
        self.twins, self.native_links = self._natives() if listed else (set[str](), set[str]())
        self.dates = self._other_lists_dates() if listed else dict[str, datetime]()
        # Short links, and where they lead, as this run knows them; how many more it may ask.
        self.links: dict[str, str] = {}
        self.follow_left = FOLLOW_NOW
        self.follow_failed = 0
        self.details_tried = 0
        self.details_failed = 0
        self.last_error = ""

    def _natives(self) -> tuple[set[str], set[str]]:
        """The jobs jobbot read lately on their own boards, by company and title (twin_key) and by link
        (job_link): a list names the company and words the title its own way, but links to the same job."""
        twins: set[str] = set()
        links: set[str] = set()
        for posting in self.store.recent_postings(self.now - _TWIN_WINDOW):
            if posting.source_id.startswith("github_list:"):
                continue
            twins.add(twin_key(posting.company, posting.title))
            links.update(key for key in (job_link(posting.url), job_link(posting.apply_url)) if key)
        return twins, links

    def _other_lists_dates(self) -> dict[str, datetime]:
        """The earliest date the other lists give each job, by its link (link_key)."""
        dates: dict[str, datetime] = {}
        for url, posted in self.store.list_dates(besides=self.source["id"]):
            key = job_link(url)
            if key is not None and (key not in dates or posted < dates[key]):
                dates[key] = posted
        return dates

    def take(self, listings: Sequence[Listing]) -> bool:
        """Every listing, until the source's time is up. True if all of them were taken."""
        stop = time.monotonic() + TIME_LIMIT
        if self.stop_by is not None:
            stop = min(stop, self.stop_by)
        complete = True
        for listing in listings:
            if time.monotonic() > stop:
                self.result.partial, complete = True, False
                break
            try:
                self._one(listing)
            except Exception:  # one bad listing never stops the rest
                log.exception("%s: couldn't take listing %s", self.source["id"], listing.external_id)
        self.store.record_skipped(self.skipped)
        self.store.record_list_rows(self.list_rows)
        self.store.used_short_links(self.links, self.now)
        self.result.skipped = len(self.skipped)
        self._describe(stop)
        return complete

    def _one(self, listing: Listing) -> None:
        pid = posting_id(listing.kind, listing.org, listing.external_id or listing.url)
        self.seen_ids.add(pid)
        why = {user["user_id"]: skip_reason(listing.title, user["settings"]) for user in self.users}
        for user_id, said in why.items():
            if said is not None:
                skips = self.result.skipped_for.setdefault(user_id, Skips())
                skips.count += 1
                if len(skips.examples) < EXAMPLES:
                    skips.examples.append((listing.company, listing.title, said))
        if all(said is not None for said in why.values()):
            self.skipped.append(
                SkippedRow(
                    listing.source_id,
                    listing.external_id,
                    listing.company,
                    listing.title,
                    listing.location,
                    self.now,
                )
            )
            return
        if listing.kind == "github_list":
            followed = self._followed(listing)
            if followed is None:
                self.result.partial = True
                return
            listing = followed
            self.list_rows.append(
                ListRow(
                    listing.source_id, listing.external_id, listing.company, listing.title, listing.url,
                    listing.alias, listing.posted_at, self.now,
                )
            )  # fmt: skip
        if self._native_copy_exists(listing):
            return
        row = self.store.known(pid)
        if listing.kind == "github_list":
            listing = self._dated(listing, row)
        hashed = listing_hash(listing)
        if row is not None and row.listing_hash == hashed and row.record_json is not None:
            if row.closed_at is None:
                self.unchanged.append(pid)
                return
            stored = row.posting()
            if stored is not None and stored.deadline is not None and stored.deadline <= self.now:
                return  # closed because its deadline passed; still listed, but it stays closed
            if stored is not None:
                self._keep(replace(stored, closed_at=None), hashed, changed=True)
                return
        self._build(listing, row, hashed)

    def _followed(self, listing: Listing) -> Listing | None:
        """The row with its short link replaced by the job's own link, or None while that isn't known: it
        waits for the next run. A link that fails waits alone; once the host turns the run away, or a few
        links fail, the rest wait too."""
        if not is_short(listing.url):
            return listing
        link = self.links.get(listing.url) or self.store.short_link(listing.url)
        if link is None:
            if self.follow_left <= 0:
                return None
            self.follow_left -= 1
            try:
                link = self.http.location(
                    listing.url, what=f"the link to {listing.company}'s {listing.title}"
                )
            except (NotThere, Refused):
                link = listing.url  # it leads nowhere: the row is kept as the list has it
            except (TurnedAway, Waits) as error:
                log.warning("%s: short links wait for the next run: %s", self.source["id"], error)
                self.follow_left = 0
                return None
            except SourceError as error:
                log.warning("%s: a short link waits for the next run: %s", self.source["id"], error)
                self.follow_failed += 1
                if self.follow_failed >= FOLLOW_FAILURES:
                    self.follow_left = 0
                return None
            self.store.keep_short_link(listing.url, link, self.now)
        self.links[listing.url] = link
        return replace(listing, url=link, alias=native_id(link) or listing.alias)

    def _dated(self, listing: Listing, row: KnownRow | None) -> Listing:
        """A list dates a job the day it added it, so the job was posted by the earliest day a list gives,
        or gave: the date it's stored with stands once the other list's row is gone."""
        key = job_link(listing.url)
        stored = row.posting() if row is not None else None
        dates = [
            listing.posted_at,
            self.dates.get(key) if key is not None else None,
            stored.posted_at if stored is not None else None,
        ]
        earliest = min((d for d in dates if d is not None), default=None)
        return listing if earliest == listing.posted_at else replace(listing, posted_at=earliest)

    def _native_copy_exists(self, listing: Listing) -> bool:
        if listing.kind != "github_list":
            return False
        if listing.alias and self.store.known(listing.alias) is not None:
            return True
        link = job_link(listing.url)
        if link is not None and link in self.native_links:
            return True
        return twin_key(listing.company, listing.title) in self.twins

    def _build(self, listing: Listing, row: KnownRow | None, hashed: str) -> None:
        if listing.kind == "github_list":
            self.to_describe.append((listing, row, hashed))
            return
        detail = None
        if self.resting:
            self.result.partial = True
            return
        if listing.kind in _DETAIL_REQUESTS:
            if self.budget <= 0:
                self.result.partial = True
                return
            self.budget -= 1
        self.details_tried += 1
        try:
            detail = READERS[listing.kind].detail(listing, self.http)
        except (TurnedAway, Waits) as error:  # not a failure of the board's: the rest waits for the next run
            log.warning("%s: details wait for the next run: %s", self.source["id"], error)
            self.resting = self.result.partial = True
            return
        except SourceError as error:
            log.warning("%s: no details for %s: %s", self.source["id"], listing.external_id, error)
            self.details_failed += 1
            self.last_error = str(error)
            return  # still new next time, so it's tried again
        first_seen = row.first_seen_at if row is not None else self.now
        posting = to_posting(listing, detail, first_seen)
        if row is not None and row.content_hash == posting.content_hash and row.closed_at is None:
            self.store.upsert_known(posting, hashed, self.now)
            return
        self._keep(posting, hashed, changed=row is not None)

    def _describe(self, stop: float) -> None:
        """Read the new and changed list rows' pages, 8 at once, until the source's time is up, and keep each
        posting. One not read is kept as the list has it, and described later in the run's spare time."""
        now = self.to_describe[: self.describe_now]
        texts = describe_all(
            self.http,
            [listing.url for listing, _, _ in now],
            min(stop, time.monotonic() + DESCRIBE_FOR),
            [listing.location for listing, _, _ in now],
        )
        texts += [None] * (len(self.to_describe) - len(now))
        for (listing, row, hashed), text in zip(self.to_describe, texts, strict=True):
            try:
                self._keep_listed(listing, row, hashed, text)
            except Exception:  # one bad listing never stops the rest
                log.exception("%s: couldn't take listing %s", self.source["id"], listing.external_id)

    def _keep_listed(self, listing: Listing, row: KnownRow | None, hashed: str, text: Read) -> None:
        """Keep a list posting with what its page said (describe_all)."""
        description, described_at, location = listing.description or "", None, listing.location
        stored = row.posting() if row is not None and row.described_at is not None else None
        if (
            row is not None
            and stored is not None
            and self.links.get(stored.url, stored.url) == listing.url  # the same page, by its short link
            and not (isinstance(text, Page) and text.text)
        ):
            # Its page said something before and nothing now: what it said still stands, and so does the
            # country its job's own record corrected, when the list still names the same places.
            description, described_at = stored.description, row.described_at
            kept = countries_in(stored.location)
            if kept and keep_countries(listing.location, kept) == stored.location:
                location = stored.location
            elif names_a_country(listing.location) and stored.location != listing.location:
                described_at = None  # the list names other places now: its record is asked again later
        elif isinstance(text, Page):
            description, described_at = with_page(description, text.text), self.now
            if text.country is not None:
                location = keep_countries(listing.location, [text.country])
        detail = replace(from_listing(listing), description=description, location=location)
        posting = to_posting(listing, detail, row.first_seen_at if row is not None else self.now)
        if row is not None and row.content_hash == posting.content_hash and row.closed_at is None:
            self.store.upsert_known(posting, hashed, self.now)
        else:
            self._keep(posting, hashed, changed=row is not None)
        self.store.described(posting.id, described_at)

    def _keep(self, posting: Posting, hashed: str, changed: bool) -> None:
        # Queued first: a posting the outbox refuses isn't stored either, so it's tried again next run.
        self.store.enqueue("posting", posting.to_record())
        self.store.upsert_known(posting, hashed, self.now)
        (self.result.postings_changed if changed else self.result.postings_new).append(posting)

    def close_missing(self, by_absence: bool, at_once: bool = False) -> int:
        """Close what's past its deadline and, when the answer was the whole board, what's missing twice
        (or at once, when the board has emptied). Return how many stay open."""
        rows = self.store.open_postings(self.source["id"])
        for row in rows:
            posting = row.posting()
            deadline = posting.deadline if posting is not None else None
            if deadline is not None and deadline <= self.now:
                self._close(row, posting, deadline)
            elif row.posting_id in self.seen_ids or not by_absence:
                continue
            elif at_once or row.missing_count + 1 >= 2:
                self._close(row, posting, self.now)
            else:
                self.store.set_missing(row.posting_id, row.missing_count + 1)
        return len(rows) - len(self.result.closed)

    def _close(self, row: KnownRow, posting: Posting | None, at: datetime) -> None:
        self.store.enqueue("closed", {"posting_id": row.posting_id, "closed_at": stamp(at)})
        if posting is None:  # pruned: closing needs only the id and the time
            self.store.close_known(row.posting_id, at, row.missing_count + 1)
        else:
            closed = replace(posting, closed_at=at)
            self.store.upsert_known(closed, row.listing_hash, row.last_seen_at, row.missing_count + 1)
        self.result.closed.append(row.posting_id)

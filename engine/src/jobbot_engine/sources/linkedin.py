"""LinkedIn's jobs, through the public job search anyone sees without signing in.

Most business jobs (project, product, marketing, finance) are posted there and nowhere jobbot reads: on
2026-11-07, 20 entry-level ones in Canada from a week came from 15 employers, none of them a source. It holds
every job, so it's only searched: for each person's role words, whatever their kind of job, in each of their
countries, each with a word of a kind they chose ("entry level marketing"). Its search ignores its own
Experience level filter: on 2026-11-08 "marketing" in Canada answered the same senior jobs with it and
without, and an entry-level person kept 5 of 40; as "entry level marketing" they kept 34 of 40. Its terms
don't welcome automated reading, so it's read lightly: WAIT seconds between requests and at most REQUESTS
a run. Each search (a word in a country) gets its turn: a run takes the next ones after those it answered
before. Taken at random, one of 120 waited four days (2026-11-12). When it turns requests away, what was read
already is kept, and the searches it didn't finish come first next run.
Its search ignores its newest-first order too: an answer comes most relevant first, its dates mixed. On
2026-11-14 a job posted that day was about 75th of the week's "project coordinator" in Canada, where only the
first 20 were read. So each search is read over the time since it was last read whole, and an hour more for
jobs it lists late (a week when it never was): a few pages, read to the last. A job missing from it isn't
closed. A result has the job's title, employer, place and date; its page isn't read.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import UTC, datetime
from time import monotonic, sleep
from urllib.parse import urlencode

from jobbot_engine.model import Listing, SourceKind
from jobbot_engine.sources.base import Detail, Openings, RoleSearch, Wants, from_listing, in_turn
from jobbot_engine.sources.http import Http, SourceError, TurnedAway, Waits
from jobbot_engine.sources.workday import QUERIES

log = logging.getLogger(__name__)

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
# An hourly run of 20 requests reads about 42 searches (2026-11-15) every 7 hours or so: a few pages each.
WAIT = 5
REQUESTS = 20
PER_PAGE = 10  # a search answers ten at a time: a shorter page is the last
# The most pages read of one search, fewer than a run's requests, so a search cut short fits the next run. One
# with more pages still counts as read: its window's 50 most relevant are read, and its turn doesn't stall.
# "entry level project" in Canada over 6 hours was 38 jobs (2026-11-15), so most are read to the last.
PAGES = 5
WEEK = 7 * 24 * 3600
LATE = 3600  # seconds more than since the last read, for jobs LinkedIn lists after their date
# Seconds the wait and a request may take (Http reads for up to 20): none starts closer than this to the run's
# reading time, since an answer after it is thrown away. A request retried after a failure can still be late,
# rarely: that run's reading is lost, and the next asks the same searches over the same times.
LEEWAY = WAIT + 25
# The word each kind of job puts in a search. New grad and Entry level search the same: LinkedIn's new-grad
# jobs say entry level. With Kind of job off ("Any"), a role word is searched alone.
KIND_WORDS = {
    "Internship": "intern",
    "Co-op": "co-op",
    "Apprenticeship": "apprentice",
    "New grad": "entry level",
    "Entry level": "entry level",
    "Part-time": "part time",
    "Contract": "contract",
}

_CARD = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"(.*?)(?=data-entity-urn=|\Z)', re.S)
_TITLE = re.compile(r'class="base-search-card__title"[^>]*>(.*?)</h3>', re.S)
_COMPANY = re.compile(r'class="base-search-card__subtitle"[^>]*>(.*?)</h4>', re.S)
_PLACE = re.compile(r'class="job-search-card__location"[^>]*>(.*?)</span>', re.S)
_DATE = re.compile(r'<time[^>]*datetime="(\d{4}-\d{2}-\d{2})"')
_TAG = re.compile(r"<[^>]+>")


class LinkedIn:
    kind: SourceKind = "linkedin"
    by_role = True  # searched for its people's roles whatever their kind of job

    def openings(self, org: str, company: str, http: Http) -> Openings:
        return self.search(org, company, http, Wants())

    def search(self, org: str, company: str, http: Http, wants: Wants) -> Openings:
        """Each search in turn, read whole over the time since it was last read, until a run's requests or its
        reading time run out. Without the run's time (a pasted link's check), a first page of each."""
        searches = in_turn(_searches(wants), wants.start)
        found: dict[str, Listing] = {}
        wanted = {_key(search) for search in searches}  # a search no one wants now is forgotten
        read_at = {key: when for key, when in wants.read_at.items() if key in wanted}
        asked = taken = 0  # requests made, and searches done with
        answered = False
        failed: SourceError | None = None

        def cut(pages: int) -> Openings:
            """What was read when the run stops part way through a search. That search keeps its time and
            goes first next run, unless it was the run's first: a search that never fits a run (a week of a
            broad word, a run started late, turned away part way) would hold every other search back."""
            done = taken + (taken == 0 and pages > 0)
            return Openings(list(found.values()), complete=False, taken=done, read_at=read_at)

        for word, place in searches:
            window = _window(wants.read_at.get(_key((word, place))), wants.now)
            whole = False
            for n in range(PAGES if wants.now else 1):
                if asked >= REQUESTS or (wants.stop_by is not None and monotonic() + LEEWAY > wants.stop_by):
                    return cut(n)  # n of its pages read
                if asked:
                    sleep(WAIT)
                asked += 1
                url = f"{SEARCH}?{urlencode(_params(word, place, window, n * PER_PAGE))}"
                try:
                    text = http.get_text(url, what=f"LinkedIn's search for {word} in {place}")
                except (TurnedAway, Waits):  # asking us to slow down, or the run's time is out
                    if not answered:
                        raise
                    return cut(n)  # keep what it read
                except SourceError as error:  # this search fails; the others are still read
                    log.warning("LinkedIn's search for %s in %s failed: %s", word, place, error)
                    failed = error
                    break
                answered = True
                cards = [_listing(org, number, card) for number, card in _CARD.findall(text)]
                for listing in cards:
                    if listing is not None:
                        found.setdefault(listing.external_id, listing)
                if len(cards) < PER_PAGE:
                    whole = True
                    break
            else:
                whole = True  # PAGES full pages: its most relevant are read
            taken += 1  # a search that failed keeps its time too, but doesn't hold the others back
            if whole and wants.now:
                read_at[_key((word, place))] = wants.now.isoformat()
        if failed is not None and not answered:
            raise failed
        return Openings(list(found.values()), complete=False, taken=taken, read_at=read_at)

    def detail(self, listing: Listing, http: Http) -> Detail:
        return from_listing(listing)


def _searches(wants: Wants) -> list[tuple[str, str]]:
    """Every search (words, country) its people want, each person's role words with their own kinds in their
    own countries, each once, in a fixed order; internships worldwide when no one has a role."""
    groups = wants.groups or (RoleSearch(wants.roles, wants.levels, wants.where),)
    pending: dict[tuple[str, str], None] = {}
    for group in groups:
        words = [f"{kind} {role}".strip() for role in group.roles for kind in _kind_words(group.levels)]
        places = sorted(group.where) if group.where else ["Worldwide"]
        pending.update(((word, place), None) for word in words for place in places)
    return list(pending) or [(word, "Worldwide") for word in QUERIES]


def _kind_words(kinds: frozenset[str]) -> list[str]:
    """The words the chosen kinds put in a search, in KIND_WORDS' order, and "" (the role word alone) for
    someone with Kind of job off ("Any") or when no kind is named. Kinds are everyone's who reads it together,
    so one person's Kind of job off doesn't take the others' kinds out."""
    words = [word for kind, word in KIND_WORDS.items() if kind in kinds]
    return list(dict.fromkeys([*words, *([""] if "Any" in kinds or not words else [])]))


def _key(search: tuple[str, str]) -> str:
    return "|".join(search)


def _window(last: str | None, now: datetime | None) -> int:
    """Seconds back a search reads: since it was last read whole and LATE more, at most a week."""
    if last is None or now is None:
        return WEEK
    return min(WEEK, max(0, int((now - datetime.fromisoformat(last)).total_seconds())) + LATE)


def _params(word: str, place: str, window: int, start: int) -> dict[str, str]:
    return {"keywords": word, "location": place, "f_TPR": f"r{window}", "start": str(start)}


def _text(found: re.Match[str] | None) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", found[1])).split()) if found else ""


def _listing(org: str, number: str, card: str) -> Listing | None:
    title = _text(_TITLE.search(card))
    if not title:
        return None
    date = _DATE.search(card)
    return Listing(
        source_id=f"linkedin:{org}",
        kind="linkedin",
        org=org,
        external_id=number,
        company=_text(_COMPANY.search(card)) or "An employer on LinkedIn",
        title=title,
        location=_text(_PLACE.search(card)),
        url=f"https://www.linkedin.com/jobs/view/{number}",
        posted_at=datetime.fromisoformat(date[1]).replace(tzinfo=UTC) if date else None,
        description="",
    )

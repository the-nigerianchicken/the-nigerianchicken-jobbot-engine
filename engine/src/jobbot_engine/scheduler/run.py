"""One run: read the sources that are due, judge what changed, send it all, and say how it went.

Finding goes first and alone: the boards, then the lists, judging what they found, and sending it, which is
what makes the app's alerts. Only after all of that is sent does the rest get the time left: the comparison
with the lists, judging open postings again after a change of rules, then reading the pages of list postings
stored without a description. None can delay or cut short a board, and what they don't finish carries on
next run.

The run summary is sent last, after everything it describes, and doubles as the heartbeat the app's watchdog
reads; when the run's time ran out before everything was sent, it goes ahead of what waits. A run that
finds another still going leaves everything alone.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import zip_longest
from typing import Any

from jobbot_engine import __version__
from jobbot_engine.api_client.client import ApiClient, ApiError, ContractError, FlushResult, flush
from jobbot_engine.coverage.compare import report
from jobbot_engine.coverage.misses import act
from jobbot_engine.coverage.moved import moved
from jobbot_engine.model import Posting, content_hash, stamp
from jobbot_engine.places import keep_countries
from jobbot_engine.repeats import Copies
from jobbot_engine.scheduler import decide
from jobbot_engine.scheduler.decide import decide_for_user, settings_key, users_by_source
from jobbot_engine.scheduler.due import due_sources
from jobbot_engine.scheduler.read import EXAMPLES, SourceResult, read_source
from jobbot_engine.sources.describe import Page, describe_all, with_page
from jobbot_engine.sources.http import Http
from jobbot_engine.store.db import KnownRow, Store
from jobbot_engine.store.outbox import MOST, OutboxKind
from jobbot_engine.upgrade import upgrade_settings

log = logging.getLogger(__name__)

WORKERS = 8
# The run's clock, in seconds from its start, so it ends inside its unit's 4 minutes (240 s). No board starts
# after READ_START_BY, a board's reading stops at READ_STOP_BY, and nothing is asked of a board after
# ASK_UNTIL (an answer still arriving is cut off then). The lists always start, after the last board, and are
# read and asked until LISTS_UNTIL: a list is thousands of rows, about 2 s each on a laptop. What waits is
# read next run, as the oldest due, and the summary says what waited and why. The comparison starts, and
# judging open postings again (there can be 11,000) goes on, only until OTHER_UNTIL. No batch is sent after
# SEND_UNTIL: the rest waits, in order, to go first next run. The summary, the app's heartbeat, goes ahead of
# what waits, until SUMMARY_UNTIL: a request may take 15 s.
READ_START_BY = 100.0
READ_STOP_BY = 130.0
ASK_UNTIL = 150.0
LISTS_UNTIL = 180.0
OTHER_UNTIL = 200.0
SEND_UNTIL = 218.0
SUMMARY_UNTIL = 224.0
# The kinds of record finding sends: postings, their closing, and decisions, which make the app's alerts.
FOUND: tuple[OutboxKind, ...] = ("posting", "decision", "closed")
WINDOW = timedelta(days=30)
COVERAGE_EVERY = timedelta(minutes=15)
# List postings stored without a description whose pages a run reads, least tried and newest first. With a
# run every 5 minutes, the 6,120 open when descriptions began to be read take about half a day.
DESCRIBE_EACH = 40
# Runs a list posting's page is tried in, while it fails, before it's left without a description.
DESCRIBE_TRIES = 12
Clock = Callable[[], datetime]


def run_once(api: ApiClient, store: Store, http: Any, now: Clock) -> dict[str, Any] | None:
    """The run's summary, or None if it couldn't start: another run holds the lock, or there's no config."""
    started = now()
    owner = str(uuid.uuid4())
    if not store.try_lock(owner, started):
        held = json.loads(store.get_meta("lock") or "{}")
        log.warning("previous run still going since %s", held.get("since"))
        return None
    try:
        return _Run(api, store, http, now, started, owner).go()
    finally:
        store.unlock(owner)
        if isinstance(http, Http):
            http.close()


class _Run:
    def __init__(
        self,
        api: ApiClient,
        store: Store,
        http: Any,
        now: Clock,
        started: datetime,
        owner: str,
    ) -> None:
        self.api, self.store, self.http, self.now = api, store, http, now
        self.started, self.run_id = started, owner
        self.clock = clock = time.monotonic()
        self.other_by = clock + OTHER_UNTIL
        self.send_by, self.summary_by = clock + SEND_UNTIL, clock + SUMMARY_UNTIL
        self.start_by, self.stop_by = clock + READ_START_BY, clock + READ_STOP_BY
        self._ask_until(ASK_UNTIL, start=True)
        self.problems: list[dict[str, str]] = []
        self.results: list[SourceResult] = []
        self.users: dict[str, list[dict[str, Any]]] = {}
        self.checked: dict[str, str] = {}
        # Sources left unread for the next run, by why.
        self.waiting: dict[str, list[str]] = {}
        # Sources with no successful check before this run: what they find now sends no alerts.
        self.baseline: set[str] = set()
        # The open postings by job, for Repeats: kept up to date as sources are read.
        self.copies = Copies(store.open_copies())

    def go(self) -> dict[str, Any] | None:
        config = self._config()
        if config is None:
            log.error("the run can't start: the app gave no config, and none was saved from an earlier run")
            return None
        self.users = users_by_source(config)
        due = due_sources(config, self.store, self.started)
        self._read(config, [s for s in due if s["kind"] != "github_list"], self.start_by, self.stop_by)
        # The lists come last, so the native copies found this run are known when the rows are compared. They
        # always start: behind slow boards they'd otherwise never be read.
        self._ask_until(LISTS_UNTIL)
        lists = [s for s in due if s["kind"] == "github_list"]
        self._read(config, lists, None, self.clock + LISTS_UNTIL)
        self._sweep(config)
        found = self._send_found()
        if found.error:
            self._problem("api", f"Some of what this run found couldn't be sent yet: {found.error}")
        # The rest of the run's work gets the time left, and only once what it found is all sent. A record the
        # app refused isn't waiting on the app: the last flush, taking the rest, sets it aside.
        went = found.error is None or found.refused
        self._ask_until(OTHER_UNTIL)
        if any(source["kind"] == "github_list" for source in config["sources"]):
            self._coverage(config, found_sent=went)
        if went:
            self._rejudge(config)
            self._describe(config)
            rest = flush(self.store, self.api, until=self.send_by)
            if rest.error:
                self._problem(
                    "api", f"The comparison or judging again couldn't all be sent yet: {rest.error}"
                )
        # Records set aside by this flush or by the last run's final one, which no summary has named yet.
        count, reason = self.store.set_aside_news()
        if count:
            noun = "1 record the app refused was" if count == 1 else f"{count} records the app refused were"
            self._problem("api", f"{noun} set aside, kept 30 days; the first: {reason}"[:500])
        for why, waiting in self.waiting.items():
            noun = "1 source waits" if len(waiting) == 1 else f"{len(waiting)} sources wait"
            names = ", ".join(sorted(waiting)[:10]) + (", and more" if len(waiting) > 10 else "")
            self._problem("sources", f"{noun} for the next run, unread ({why}): {names}")
        partial = sorted(source["company"] for source in due if self._partial(source["id"]))
        if partial:
            noun = "1 source was" if len(partial) == 1 else f"{len(partial)} sources were"
            names = ", ".join(partial[:10]) + (", and more" if len(partial) > 10 else "")
            self._problem(
                "sources", f"{noun} read only in part this run, and the rest of it is read next run: {names}"
            )
        summary = self._summary(len(due))
        self.store.enqueue("run", summary)
        # Everything else went just before, or waits for the next run.
        flush(self.store, self.api, until=self.summary_by, seqs=self.store.outbox_seqs("run"))
        self.store.prune(self.now())
        return summary

    def _ask_until(self, seconds: float, start: bool = False) -> None:
        """Ask nothing of a board after this point of the run's clock."""
        if isinstance(self.http, Http):
            if start:
                self.http.start_run(self.clock + seconds)
            else:
                self.http.stop_asking_at(self.clock + seconds)

    def _send_found(self) -> FlushResult:
        """Send the records waiting of the kinds finding sends, oldest first."""
        seqs = sorted(seq for kind in FOUND for seq in self.store.outbox_seqs(kind))
        return flush(self.store, self.api, until=self.send_by, seqs=seqs)

    def _partial(self, source_id: str) -> bool:
        """Whether this run read the source only in part (its time ran out, or its details budget)."""
        return any(r.source_id == source_id and r.partial and r.error is None for r in self.results)

    def _coverage(self, config: Mapping[str, Any], found_sent: bool) -> None:
        """Every 15 minutes: compare the sources with the GitHub lists, act on the misses, look for where
        broken boards went, and report."""
        last = self.store.get_meta("coverage_at")
        if last is not None and self.started - datetime.fromisoformat(last) < COVERAGE_EVERY:
            return
        if not found_sent:  # it waits behind what the run found
            self._problem(
                "coverage",
                "The comparison with the lists waits for the next run: what this run found isn't sent yet",
            )
            return
        if time.monotonic() > self.other_by:  # due, but its lookups would run past the run's time
            self._problem(
                "coverage", "The comparison with the lists waits for the next run: this run's time ran out"
            )
            return
        try:
            doc = report(self.store, config, self.started)
            doc["missed"], added = act(doc["missed"], config, self.store, self.http, self.started)
            doc["moved"] = moved(config, self.store, self.http, self.started)
            if added is not None:
                self.store.enqueue("sources", added)
            self.store.enqueue("coverage", doc)
            self.store.set_meta("coverage_at", self.started.isoformat())
        except Exception as error:  # the comparison failing never stops the run
            log.exception("the comparison with the GitHub lists failed")
            self._problem("coverage", f"The comparison with the GitHub lists couldn't be made: {error}")

    def _sweep(self, config: Mapping[str, Any]) -> None:
        """Decide the open postings someone has no decision about: left by a run cut short between storing a
        posting and deciding it, or on a source they resumed or added after it was found."""
        since = self.started - WINDOW
        for user in config["users"]:
            sources = {use["source_id"] for use in user["sources"] if not use["paused"]}
            for posting in self.store.undecided(user["user_id"], sources, since):
                baseline = posting.source_id in self.baseline
                record = decide_for_user(
                    user, posting, self.store, self.started, baseline, copies=self.copies
                )
                if record is not None:
                    self._decision(record)

    def _decision(self, record: dict[str, Any]) -> None:
        self.store.enqueue("decision", record)

    def _problem(self, area: str, message: str) -> None:
        if len(self.problems) < 20:
            self.problems.append({"area": area, "message": message[:500]})

    def _config(self) -> dict[str, Any] | None:
        try:
            config = self.api.engine_config(checked=self.store.get_meta("config_digest"))
        except (ApiError, ContractError) as error:
            saved = self.store.get_meta("last_config")
            if saved is None:
                return None
            self._problem("api", f"The app couldn't give its config, so the last one was used: {error}")
            loaded: dict[str, Any] = json.loads(saved)
            # An earlier engine saved it, so its settings may be in an older form. One person's that
            # can't be read leaves out only that person.
            users: list[dict[str, Any]] = []
            for user in loaded["users"]:
                try:
                    user["settings"] = upgrade_settings(user["settings"])
                except (KeyError, TypeError, ValueError) as bad:
                    self._problem("api", f"{user['user_id']}'s saved settings couldn't be read: {bad!r}")
                    continue
                users.append(user)
            loaded["users"] = users
            return loaded
        self.store.set_meta("last_config", json.dumps(config))
        self.store.set_meta("config_digest", self.api.config_digest or "")
        return config

    def _rejudge(self, config: Mapping[str, Any]) -> None:
        """Judge again the open postings of anyone whose rules or follows changed, or everyone's when
        jobbot's judging changed; that time without alerts. It stops at OTHER_UNTIL, and the next run
        carries on with the postings not yet judged under the new settings; only then is it marked done.
        Its decisions are sent a batch at a time, so at most one batch left unsent goes ahead of a
        later run's finds."""
        recent = None
        judged = 0
        for user in config["users"]:
            user_id = user["user_id"]
            key, meta = settings_key(user), f"settings_key:{user_id}"
            if self.store.get_meta(meta) == key:
                continue
            judged_by, judge_meta = str(decide.JUDGE), f"judge:{user_id}"
            quiet = self.store.get_meta(meta) is not None and self.store.get_meta(judge_meta) != judged_by
            if recent is None:
                recent = [p for p in self.store.recent_postings(self.started - WINDOW) if p.closed_at is None]
            sources = {use["source_id"] for use in user["sources"] if not use["paused"]}
            # One already judged under these settings was judged by a run that stopped part way.
            left = [
                posting
                for posting in recent
                if posting.source_id in sources and not self._judged_under(key, user_id, posting.id)
            ]
            for done, posting in enumerate(left):
                if time.monotonic() >= self.other_by:
                    log.info("judged %d of %d open postings again; the next run carries on", done, len(left))
                    return
                record = decide_for_user(
                    user, posting, self.store, self.started, quiet=quiet, copies=self.copies
                )
                if record is not None:
                    self.store.enqueue("decision", record)
                    judged += 1
                    if judged % MOST == 0 and self._send_found().error:
                        return  # the app or the time ran out: the rest is judged next run
            self.store.set_meta(meta, key)
            self.store.set_meta(judge_meta, judged_by)

    def _describe(self, config: Mapping[str, Any]) -> None:
        """Read the pages of list postings stored without a description, until OTHER_UNTIL, and judge each one
        whose description changed again, without alerts: it was found before, so it isn't news."""
        lists = {source["id"] for source in config["sources"] if source["kind"] == "github_list"}
        if not lists or time.monotonic() >= self.other_by:
            return
        rows = [(row, row.posting()) for row in self.store.undescribed(lists, DESCRIBE_EACH)]
        kept = [(row, posting) for row, posting in rows if posting is not None]
        texts = describe_all(
            self.http,
            [posting.url for _, posting in kept],
            self.other_by,
            [posting.location for _, posting in kept],
        )
        for (row, posting), text in zip(kept, texts, strict=True):
            if text is None:  # not asked in time: waits for a later run
                continue
            try:
                if isinstance(text, Page):
                    self._described(row, posting, text)
                else:
                    self.store.tried(posting.id, self.now(), DESCRIBE_TRIES)
            except Exception:  # one posting failing never stops the rest
                log.exception("couldn't keep the description of %s", posting.id)

    def _described(self, row: KnownRow, posting: Posting, page: Page) -> None:
        described = replace(posting, description=with_page(posting.description, page.text))
        if page.country is not None:
            described = replace(described, location=keep_countries(posting.location, [page.country]))
        described = replace(described, content_hash=content_hash(described))
        if described.content_hash != posting.content_hash:
            self.store.enqueue("posting", described.to_record())
            self.store.upsert_known(described, row.listing_hash, row.last_seen_at, row.missing_count)
            for user in self.users.get(posting.source_id, []):
                record = decide_for_user(
                    user, described, self.store, self.started, copies=self.copies, described=True
                )
                if record is not None:
                    self._decision(record)
        self.store.described(posting.id, self.now())

    def _judged_under(self, key: str, user_id: str, posting_id: str) -> bool:
        last = self.store.last_decision(user_id, posting_id)
        return last is not None and last.settings_key == key

    def _read(
        self,
        config: Mapping[str, Any],
        sources: list[dict[str, Any]],
        start_by: float | None,
        stop_by: float,
    ) -> None:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures: dict[Future[SourceResult], dict[str, Any]] = {}
            for source in sources:
                at = self.now()
                self.checked[source["id"]] = stamp(at) or ""
                state = self.store.source_state(source["id"])
                if state is None or state.last_ok_at is None:
                    self.baseline.add(source["id"])
                users = self.users.get(source["id"], [])
                reading = (source, users, self.store, self.http, at)
                futures[pool.submit(read_source, *reading, start_by=start_by, stop_by=stop_by)] = source
            for future in as_completed(futures):
                self._take(config, futures[future], future)

    def _take(
        self, config: Mapping[str, Any], source: Mapping[str, Any], future: Future[SourceResult]
    ) -> None:
        try:
            result = future.result()
        except Exception as error:  # a bug while reading: this source fails, the run goes on
            message = f"jobbot couldn't read it: {type(error).__name__}: {error}"
            self.store.record_check(source["id"], False, message[:500], self.now(), found=0, open_count=0)
            result = SourceResult(source["id"], error=message[:500])
        if result.later is not None:  # not checked: still due, so the next run reads it first
            self.checked.pop(source["id"], None)
            self.waiting.setdefault(result.later, []).append(source["company"])
            return
        self.results.append(result)
        found = [*result.postings_new, *result.postings_changed]
        # Another copy of a job may now be the one shown, or no longer be: those are judged again too.
        others: set[str] = set()
        for posting_id in result.closed:
            others.update(self.copies.others(posting_id))
            self.copies.remove(posting_id)
        for posting in found:
            others.update(self.copies.others(posting.id))
            self.copies.add(posting)
            others.update(self.copies.others(posting.id))
        for posting in found:
            self._judge(posting, posting.source_id in self.baseline)
        for posting_id in sorted(others - {posting.id for posting in found}):
            row = self.store.known(posting_id)
            other = row.posting() if row is not None else None
            if other is not None and other.closed_at is None:
                self._judge(other, False, again=True)

    def _judge(self, posting: Posting, baseline: bool, again: bool = False) -> None:
        for user in self.users.get(posting.source_id, []):
            record = decide_for_user(
                user, posting, self.store, self.now(), baseline, copies=self.copies, again=again
            )
            if record is not None:
                self._decision(record)

    def _summary(self, due: int) -> dict[str, Any]:
        failures: list[dict[str, Any]] = []
        for result in self.results:
            if result.error:
                state = self.store.source_state(result.source_id)
                consecutive = state.consecutive_failures if state else 1
                failures.append(
                    {"source_id": result.source_id, "error": result.error, "consecutive": consecutive}
                )
        return {
            "run_id": self.run_id,
            "engine_version": __version__,
            "started_at": stamp(self.started),
            "finished_at": stamp(self.now()),
            "sources_due": due,
            "sources_ok": sum(1 for r in self.results if r.error is None),
            "failures": failures,
            "checked": self.checked,
            "postings_new": sum(len(r.postings_new) for r in self.results),
            "postings_changed": sum(len(r.postings_changed) for r in self.results),
            "postings_closed": sum(len(r.closed) for r in self.results),
            "skipped_other_kinds": sum(r.skipped for r in self.results),
            "skipped_by_person": self._skipped_by_person(),
            "problems": self.problems,
        }

    def _skipped_by_person(self) -> dict[str, Any]:
        """Each person's skips in this run, examples taken a source at a time so no one board fills them."""
        found: dict[str, dict[str, Any]] = {}
        for user_id in sorted({u for r in self.results for u in r.skipped_for}):
            mine = {r.source_id: r.skipped_for[user_id] for r in self.results if user_id in r.skipped_for}
            examples = [e for row in zip_longest(*(s.examples for s in mine.values())) for e in row if e]
            found[user_id] = {
                "by_source": {source_id: s.count for source_id, s in mine.items()},
                "examples": [
                    {"company": c[:300], "title": t[:300], "why": w[:500]} for c, t, w in examples[:EXAMPLES]
                ],
            }
        return found

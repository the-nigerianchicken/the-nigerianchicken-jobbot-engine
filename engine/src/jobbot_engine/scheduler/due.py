"""Which sources a run checks: each as often as its keenest user wants; followed ones every 5 minutes.

A source isn't checked whenever its interval has passed since the last check, or every hourly source would
come due in the same run, as they all did on the first day. Each has its own moments instead: every
`interval` minutes, starting at an offset taken from a hash of its id, rounded to the 5-minute runs. So an
hourly source is due in one run of the hour's twelve, the same one each hour, and each run reads about a
twelfth of them. A run that was missed is made up by the next, since a source is due once one of its moments
has passed since its last check.

A source never checked is due at once, but a run takes at most NEW_PER_RUN of them, those checked most often
first, so a single added source is read in the next run and a launch-day import of thousands is spread over
the first hour or so. After the server was down, every source is due at once; a run then takes at most
OVERDUE_PER_RUN of them, those waiting longest first, and the rest in the runs after. Followed sources are
never held back.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from jobbot_engine.names import follows
from jobbot_engine.store.db import Store

FOLLOWED_MINUTES = 5
RUN_MINUTES = 5  # how often the server's timer starts a run
NEW_PER_RUN = 150
# Twice an ordinary run's share of 2,250 hourly sources: never reached unless runs were missed.
OVERDUE_PER_RUN = 400
# A source checked this recently is never due, whatever its interval: it absorbs the timer's jitter.
JITTER = timedelta(minutes=4)
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def intervals(config: Mapping[str, Any]) -> dict[str, int]:
    """Each used source's interval in minutes: the shortest among the people using it and not pausing it."""
    sources = {source["id"]: source for source in config["sources"]}
    shortest: dict[str, int] = {}
    for user in config["users"]:
        followed = [follow["company"] for follow in user["follows"]]
        for use in user["sources"]:
            source = sources.get(use["source_id"])
            if source is None or use["paused"]:
                continue
            minutes: int = use["frequency_minutes"]
            if any(follows(company, source["company"], source["org"]) for company in followed):
                minutes = FOLLOWED_MINUTES
            shortest[source["id"]] = min(minutes, shortest.get(source["id"], minutes))
    return shortest


def offset(source_id: str, minutes: int) -> int:
    """Where in each of its intervals a source's moment falls, in minutes: stable, and on a run's minute."""
    if minutes <= RUN_MINUTES:
        return 0
    spread = int(hashlib.sha256(source_id.encode()).hexdigest()[:8], 16) % minutes
    return spread - spread % RUN_MINUTES


def last_moment(source_id: str, minutes: int, now: datetime) -> datetime:
    """The latest of the source's moments at or before `now`."""
    since = (now - _EPOCH) // timedelta(minutes=1)
    start = offset(source_id, minutes)
    return _EPOCH + timedelta(minutes=start + (since - start) // minutes * minutes)


def due_sources(config: Mapping[str, Any], store: Store, now: datetime) -> list[dict[str, Any]]:
    sources = {source["id"]: source for source in config["sources"]}
    due: list[dict[str, Any]] = []
    waiting: list[tuple[datetime, str]] = []
    never: list[tuple[int, str]] = []
    for source_id, minutes in intervals(config).items():
        state = store.source_state(source_id)
        last = state.last_checked_at if state else None
        if last is None:
            never.append((minutes, source_id))
        elif last < last_moment(source_id, minutes, now) and now - last >= JITTER:
            if minutes <= FOLLOWED_MINUTES:
                due.append(sources[source_id])
            else:
                waiting.append((last, source_id))
    due += [sources[source_id] for _last, source_id in sorted(waiting)[:OVERDUE_PER_RUN]]
    due += [sources[source_id] for _minutes, source_id in sorted(never)[:NEW_PER_RUN]]
    return due

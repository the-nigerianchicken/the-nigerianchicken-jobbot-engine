"""What a change of rules would do, shown before it's saved.

Every open posting of the last 30 days on the person's sources is judged under the rules they have and under
the edited ones. Postings the person has decided on are the app's to hide.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from jobbot_engine.model import Posting
from jobbot_engine.rules import Context, Outcome, collects, evaluate
from jobbot_engine.store.db import Store

WINDOW_DAYS = 30
MOST = 1000


def _judge(
    store: Store, settings: Mapping[str, Any], follows: tuple[str, ...], now: datetime
) -> Callable[[Posting], Outcome]:
    context = Context(now=now, details=settings["details"], follows=follows)

    def outcome(posting: Posting) -> Outcome:
        return evaluate(posting, settings, context)

    return outcome


def _ref(posting: Posting) -> dict[str, Any]:
    return {
        "posting_id": posting.id,
        "company": posting.company or "Unknown",
        "title": posting.title or "Untitled",
    }


def run(
    job: Mapping[str, Any],
    store: Store,
    current_settings: Mapping[str, Any],
    follows: Sequence[str],
    now: datetime,
    sources: Collection[str] | None = None,
) -> dict[str, Any]:
    edited: Mapping[str, Any] = job["input"]["settings"]
    following = tuple(follows)
    before = _judge(store, current_settings, following, now)
    after = _judge(store, edited, following, now)
    since = now - timedelta(days=WINDOW_DAYS)
    leaving: list[dict[str, Any]] = []
    returning: list[dict[str, Any]] = []
    for posting in store.recent_postings(since):
        if posting.closed_at is not None or (sources is not None and posting.source_id not in sources):
            continue
        old, new = before(posting), after(posting)
        if old.state == "new" and new.state != "new":
            leaving.append({**_ref(posting), "rule_name": new.rule_name or "A filter"})
        elif old.state != "new" and new.state == "new":
            returning.append(_ref(posting))
    more = sum(
        1
        for row in store.skipped_since(since)
        if (sources is None or row.source_id in sources)
        and not collects(row.title, current_settings)
        and collects(row.title, edited)
    )
    return {
        "window_days": WINDOW_DAYS,
        "leaving_new": leaving[:MOST],
        "newly_flagged": [],  # nothing is flagged: a rule shows or hides
        "returning": returning[:MOST],
        "collected_more": more,
    }

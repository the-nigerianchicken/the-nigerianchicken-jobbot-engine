"""Each person's decision about a posting, sent only when it changes.

A decision is sent when its state, its reason or its notes change. The engine never sends the states a person
sets; the app keeps those over anything sent here. A posting Kind of job doesn't collect is stored as skipped
and never sent, so it's sent if a later change of rules collects it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from jobbot_engine.model import Posting, stamp
from jobbot_engine.repeats import Copies, Copy
from jobbot_engine.rules import Context, Outcome, evaluate
from jobbot_engine.store.db import Store

FIRST_CHECK = "First check of this source: no alert"
QUIET = "Judged again when jobbot's rules changed: no alert"
SHOWN_BEFORE = "Another copy of this job was shown before: no alert"
DESCRIBED = "Judged again once its description was read: no alert"
# How postings are judged. Raising it judges every open posting again, once, without alerts.
# 4: plain rules in place of tags; 5: Repeats; 6: a list's copy is known by its link; 7: LinkedIn's copies
JUDGE = 7


def settings_key(user: Mapping[str, Any]) -> str:
    """What decides whether a person's earlier decisions still stand: their settings' version and follows,
    and how postings are judged."""
    basis = {
        "version": user["settings"]["version"],
        "follows": sorted(follow["company"] for follow in user["follows"]),
        "judge": JUDGE,
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()


def _digest(outcome: Outcome) -> str:
    """What the app was told: a change in any of it is sent. Whether it alerted isn't part of it: the app
    alerts only when a posting first enters New, so judging it again with an alert due says nothing new."""
    basis = {
        "state": outcome.state,
        "rule_id": outcome.rule_id,
        "matched": outcome.matched,
        "notes": [note for note in outcome.notes if note not in (FIRST_CHECK, QUIET, SHOWN_BEFORE)],
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def users_of(source_id: str, config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The people who use a source and haven't paused it."""
    return [
        user
        for user in config["users"]
        if any(use["source_id"] == source_id and not use["paused"] for use in user["sources"])
    ]


def users_by_source(config: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """`users_of` for every source at once: asking source by source read every person's list each time."""
    found: dict[str, list[dict[str, Any]]] = {}
    for user in config["users"]:
        for source_id in dict.fromkeys(use["source_id"] for use in user["sources"] if not use["paused"]):
            found.setdefault(source_id, []).append(user)
    return found


def _cut(text: str, most: int) -> str:
    return text if len(text) <= most else text[: most - 1] + "…"


def _record(user: Mapping[str, Any], posting: Posting, outcome: Outcome, now: datetime) -> dict[str, Any]:
    return {
        "user_id": user["user_id"],
        "posting_id": posting.id,
        "state": outcome.state,
        "rule_id": outcome.rule_id,
        "rule_name": _cut(outcome.rule_name, 80) if outcome.rule_name else None,
        "matched": [_cut(word, 200) for word in outcome.matched][:20],
        "settings_version": user["settings"]["version"],
        "evaluated_at": stamp(now),
        "alert_reason": _cut(outcome.alert_reason, 200) if outcome.alert_reason else None,
        "notes": [_cut(note, 200) for note in outcome.notes if note][:10],
    }


def _shown_by(settings: Mapping[str, Any], context: Context, store: Store) -> Callable[[Copy], bool]:
    """Whether the person's rules, Repeats aside, leave a copy of a job shown."""

    def shown(copy: Copy) -> bool:
        row = store.known(copy.id)
        other = row.posting() if row is not None else None
        return other is not None and evaluate(other, settings, context).state == "new"

    return shown


def decide_for_user(
    user: Mapping[str, Any],
    posting: Posting,
    store: Store,
    now: datetime,
    baseline: bool = False,
    quiet: bool = False,
    copies: Copies | None = None,
    again: bool = False,
    described: bool = False,
) -> dict[str, Any] | None:
    """The person's decision record if it changed, after storing it; None if it didn't or isn't sent.

    On a source's first successful check (`baseline`) nothing alerts: what's there is
    years of postings the engine has never seen, not news. Nor does anything when jobbot's own judging
    changed (`quiet`): what it finds again isn't news either. `copies` are the open postings, for Repeats;
    a copy of a job shown before isn't news, whether it was found later or is judged `again` because another
    copy closed or changed. Nor is a list posting judged again once its page is read (`described`).
    """
    settings = user["settings"]
    follows = tuple(follow["company"] for follow in user["follows"])
    context = Context(now=now, details=settings["details"], follows=follows)
    sources = {use["source_id"] for use in user["sources"] if not use["paused"]}
    shown = _shown_by(settings, context, store)
    if copies is not None:
        context = replace(context, shown_instead=copies.shown_instead(posting, sources, shown))
    outcome = evaluate(posting, settings, context)
    if outcome.alert_reason:
        said = FIRST_CHECK if baseline else QUIET if quiet else DESCRIBED if described else None
        if said is None and (again or (copies is not None and copies.found_before(posting, sources, shown))):
            said = SHOWN_BEFORE
        if said is not None:
            outcome = replace(outcome, alert_reason=None, notes=(*outcome.notes, said))
    key, digest = settings_key(user), _digest(outcome)
    last = store.last_decision(user["user_id"], posting.id)
    if last is not None and last.digest == digest:
        if last.settings_key != key:
            store.save_decision(user["user_id"], posting.id, outcome.state, outcome.rule_id, key, digest)
        return None
    record = None
    if outcome.state != "skipped":
        record = _record(user, posting, outcome, now)
    elif last is not None and last.state != "skipped":
        # Kind of job narrowed: the app still shows the posting, so it's told, as hidden by Kind of job.
        record = _record(user, posting, replace(outcome, state="filtered"), now)
    store.save_decision(user["user_id"], posting.id, outcome.state, outcome.rule_id, key, digest)
    return record


def decide_for_users(
    posting: Posting, config: Mapping[str, Any], store: Store, now: datetime
) -> list[dict[str, Any]]:
    """The decisions that changed, for each person who uses the posting's source."""
    records = (decide_for_user(user, posting, store, now) for user in users_of(posting.source_id, config))
    return [record for record in records if record is not None]

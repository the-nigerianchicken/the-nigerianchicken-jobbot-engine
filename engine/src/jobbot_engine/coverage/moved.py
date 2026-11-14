"""Where a board that stopped working went.

A board that has failed for more than a day has usually moved: Carbon Robotics left Greenhouse for Ashby,
Netflix left Workday for a site of its own. For each one, this says which source jobbot already reads the
company from, or which board Greenhouse, Lever or Ashby hosts under the company's name now, so the person can
switch to it in place of only removing the old one. A board with nowhere to go is left out, and stays one to
remove. Lookups are shared with the misses' (remembered for a day) and held to `budget` a comparison.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from jobbot_engine.coverage.misses import look_up, remembered
from jobbot_engine.model import parse_stamp
from jobbot_engine.names import key
from jobbot_engine.scheduler.decide import users_by_source
from jobbot_engine.sources.detect import board_url
from jobbot_engine.sources.http import Waits
from jobbot_engine.store.db import Store

FAILING_FOR = timedelta(days=1)  # when the screens start asking him about a board


def moved(
    config: Mapping[str, Any], store: Store, http: Any, now: datetime, budget: int = 5
) -> list[dict[str, Any]]:
    """For each board failing for more than a day: the source covering its company, or where it moved.

    Only boards someone reads count, broken or covering: a removed board keeps the state it was removed in.
    """
    read = users_by_source(config)
    sources = [s for s in config["sources"] if s["kind"] != "github_list" and s["id"] in read]
    # By lower case: a board's id is the same whatever its letter case.
    working = {s["id"].lower(): s for s in sources if s["failing_since"] is None}
    found: list[dict[str, Any]] = []
    for gone in sources:
        since = parse_stamp(gone["failing_since"])
        if since is None or now - since <= FAILING_FOR:
            continue
        covering = next(
            (s["id"] for s in working.values() if key(s["company"]) == key(gone["company"])), None
        )
        if covering is not None:
            found.append({"source_id": gone["id"], "covered_by": covering, "to": None})
            continue
        known = remembered(store, gone["company"], now)
        if known is None:
            if budget <= 0:
                continue  # the next comparison looks
            budget -= 1
            try:
                known = [look_up(store, http, gone["company"], now)]
            except Waits:  # a host asked us to slow down, or the run's time ran out
                continue
        board = known[0]
        if board is None or f"{board[0]}:{board[1]}".lower() == gone["id"].lower():
            continue
        kind, org = board
        source_id = f"{kind}:{org}"
        if source_id.lower() in working:
            covered_by = working[source_id.lower()]["id"]
            found.append({"source_id": gone["id"], "covered_by": covered_by, "to": None})
        else:
            url = board_url(kind, org, gone["url"])
            found.append(
                {"source_id": gone["id"], "covered_by": None, "to": {"kind": kind, "org": org, "url": url}}
            )
    return found

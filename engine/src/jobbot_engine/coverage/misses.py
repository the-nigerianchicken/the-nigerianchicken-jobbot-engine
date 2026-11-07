"""What's done about each posting a list had and jobbot missed.

1. A link to a board jobbot already reads: nothing to do, and the miss names that source.
2. A link to a board jobbot can read but doesn't yet: the people using the list who chose "add" get it added
   at 60 minutes; if they all chose "ask", they're asked.
3. A link to a board jobbot can't read: it's named unsupported.
4. Anything else: Greenhouse, Lever and Ashby are asked whether they host a board under the company's name,
   for at most `probe_budget` companies a run, and an answer is remembered for a day. A board found goes
   through step 2. A company past the budget, or a board past the 50 one sources-add holds, is "later":
   the next comparison gets to it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from jobbot_engine.model import SourceKind, stamp
from jobbot_engine.names import key
from jobbot_engine.scheduler.decide import users_of
from jobbot_engine.sources.detect import UNSUPPORTED_HOSTS, board_url, from_url
from jobbot_engine.sources.find_board import find
from jobbot_engine.sources.http import Waits
from jobbot_engine.store.db import Store

MISSED_MINUTES = 60
REMEMBER_PROBE = timedelta(days=1)
_MOST = 50  # sources and user-sources per sources-add


def remembered(store: Store, company: str, now: datetime) -> list[tuple[SourceKind, str] | None] | None:
    """[what a lookup within the last day found], or None when there's none to go by."""
    saved = store.get_meta(f"probe:{key(company)}")
    if saved is None:
        return None
    lookup = json.loads(saved)
    if now - datetime.fromisoformat(lookup["at"]) >= REMEMBER_PROBE:
        return None
    found = lookup["found"]
    return [(found[0], found[1]) if found else None]


def look_up(store: Store, http: Any, company: str, now: datetime) -> tuple[SourceKind, str] | None:
    """Asks Greenhouse, Lever and Ashby for the company's board, and remembers the answer for a day."""
    hit = find(company, http)
    found = (hit[0], hit[1]) if hit else None
    store.set_meta(f"probe:{key(company)}", json.dumps({"at": now.isoformat(), "found": found}))
    return found


class _Acting:
    def __init__(
        self, config: Mapping[str, Any], store: Store, http: Any, now: datetime, budget: int
    ) -> None:
        self.config, self.store, self.http, self.now, self.budget = config, store, http, now, budget
        self.known = {s["id"] for s in config["sources"]}
        self.sources: dict[str, dict[str, Any]] = {}
        self.uses: dict[tuple[str, str], dict[str, Any]] = {}

    def one(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        url: str = entry["url"]
        found = from_url(url)
        if found is None:
            host = urlparse(url).netloc.lower()
            if any(host == h or host.endswith("." + h) for h in UNSUPPORTED_HOSTS):
                return {**entry, "action": "unsupported", "source_id": None}
            if self._out_of_probes(entry["company"]):
                return {**entry, "action": "later", "source_id": None}
            try:
                found = self._probe(entry["company"])
            except Waits:  # a host asked us to slow down, or the run's time ran out: a later comparison
                return {**entry, "action": "later", "source_id": None}
            if found is None:
                return {**entry, "action": "none", "source_id": None}
        kind, org = found
        source_id = f"{kind}:{org}"
        if source_id in self.known:
            return {**entry, "action": "none", "source_id": source_id}
        return {**entry, **self._new_source(entry, kind, org, source_id)}

    def _new_source(
        self, entry: Mapping[str, Any], kind: SourceKind, org: str, source_id: str
    ) -> dict[str, Any]:
        users = users_of(entry["list_source_id"], self.config)
        adding = [u for u in users if u["settings"]["misses"] == "add"]
        if not adding:
            return {"action": "asked" if users else "none", "source_id": source_id if users else None}
        full = len(self.sources) >= _MOST or len(self.uses) + len(adding) > _MOST
        if source_id not in self.sources and full:
            # One sources-add holds 50 sources and 50 links to people; the rest go next time.
            return {"action": "later", "source_id": None}
        self.sources.setdefault(
            source_id,
            {
                "id": source_id,
                "kind": kind,
                "org": org,
                "company": entry["company"],
                "url": board_url(kind, org, entry["url"]),
                "added_at": stamp(self.now),
                "added_by": "missed_posting",
                "status": "unknown",
                "failing_since": None,
                "last_error": None,
            },
        )
        for user in adding:
            use = {"user_id": user["user_id"], "source_id": source_id, "frequency_minutes": MISSED_MINUTES}
            self.uses.setdefault((user["user_id"], source_id), {**use, "paused": False})
        return {"action": "source_added", "source_id": source_id}

    def _out_of_probes(self, company: str) -> bool:
        """Whether this company would need a lookup this run has no budget left for."""
        return self.budget <= 0 and remembered(self.store, company, self.now) is None

    def _probe(self, company: str) -> tuple[SourceKind, str] | None:
        known = remembered(self.store, company, self.now)
        if known is not None:
            return known[0]
        if self.budget <= 0:
            return None
        self.budget -= 1
        return look_up(self.store, self.http, company, self.now)

    def payload(self) -> dict[str, Any] | None:
        if not self.sources:
            return None
        return {
            "sources": list(self.sources.values())[:_MOST],
            "user_sources": list(self.uses.values())[:_MOST],
        }


def act(
    missed: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    store: Store,
    http: Any,
    now: datetime,
    probe_budget: int = 10,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The missed entries with their action and source, and the sources to add, if any."""
    acting = _Acting(config, store, http, now, probe_budget)
    return [acting.one(entry) for entry in missed], acting.payload()

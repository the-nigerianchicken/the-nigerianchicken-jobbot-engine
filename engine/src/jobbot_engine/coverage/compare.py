"""How jobbot's own sources compare with the GitHub lists.

Over the last week, of the list rows someone collects: how many jobbot also found on the job's own board
(`found`), how many of those it found before the list posted them (`found_first`), and every one it missed.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from jobbot_engine.model import stamp
from jobbot_engine.sources.github_list import link_key, twin_key
from jobbot_engine.store.db import Store
from jobbot_engine.store.rows import ListRow

# Native postings are matched to rows over a longer span than the window, since a job can be on its board
# well before a list carries it.
_NATIVE_SPAN = timedelta(days=30)


def report(store: Store, config: Mapping[str, Any], now: datetime, window_days: int = 7) -> dict[str, Any]:
    lists = [s for s in config["sources"] if s["kind"] == "github_list"]
    list_ids = {s["id"] for s in lists}
    first_seen, by_link = _native_first_seen(store, now)
    rows: dict[str, ListRow] = {}
    for row in store.list_rows_since(now - timedelta(days=window_days)):
        if row.source_id in list_ids:
            rows.setdefault(row.url, row)
    found = found_first = 0
    missed: list[dict[str, Any]] = []
    for row in rows.values():
        native = _twin(store, row, first_seen, by_link)
        if native is None:
            if not _is_link(row.url):
                continue  # a row without a usable link can't be acted on, or reported
            missed.append(
                {
                    "company": _cut(row.company, 200) or "Unknown company",
                    "title": _cut(row.title, 300) or "Untitled",
                    "url": row.url,
                    "list_source_id": row.source_id,
                    "action": "none",
                    "source_id": None,
                }
            )
            continue
        found += 1
        if row.posted_at is not None and native < row.posted_at:
            found_first += 1
    return {
        "generated_at": stamp(now),
        "window_days": window_days,
        "lists": [_list_state(store, s["id"]) for s in lists],
        "list_postings": len(rows),
        "found": found,
        "found_first": found_first,
        "missed": missed,
    }


def _cut(text: str, most: int) -> str:
    return text.strip()[:most]


def _is_link(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc) and " " not in url


def _native_first_seen(store: Store, now: datetime) -> tuple[dict[str, datetime], dict[str, datetime]]:
    """When each native posting of the last 30 days was first seen, by its twin key and by its links."""
    seen: dict[str, datetime] = {}
    by_link: dict[str, datetime] = {}
    for posting in store.recent_postings(now - _NATIVE_SPAN):
        if posting.source_id.startswith("github_list:"):
            continue
        keys = [(seen, twin_key(posting.company, posting.title))]
        keys += [(by_link, link_key(url)) for url in (posting.url, posting.apply_url)]
        for found, key in keys:
            if key is not None and (key not in found or posting.first_seen_at < found[key]):
                found[key] = posting.first_seen_at
    return seen, by_link


def _twin(
    store: Store, row: ListRow, first_seen: Mapping[str, datetime], by_link: Mapping[str, datetime]
) -> datetime | None:
    """When jobbot first saw this row's job on its own board, or None if it hasn't: by the posting the row
    names, by the job's link, or by its company and title."""
    if row.alias:
        known = store.known(row.alias)
        if known is not None and not known.source_id.startswith("github_list:"):
            return known.first_seen_at
    link = link_key(row.url)
    if link is not None and link in by_link:
        return by_link[link]
    return first_seen.get(twin_key(row.company, row.title))


def _list_state(store: Store, source_id: str) -> dict[str, Any]:
    state = store.source_state(source_id)
    if state is None:
        return {"source_id": source_id, "read_at": None, "error": None}
    error = state.last_error if state.consecutive_failures else None
    return {"source_id": source_id, "read_at": stamp(state.last_ok_at), "error": error}

"""The rows the store hands back."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from jobbot_engine.model import Posting, parse_stamp


@dataclass(frozen=True)
class KnownRow:
    posting_id: str
    source_id: str
    external_id: str
    listing_hash: str
    content_hash: str
    record_json: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    closed_at: datetime | None
    missing_count: int
    described_at: datetime | None = None

    def posting(self) -> Posting | None:
        """The stored posting, or None once it's been pruned."""
        return Posting.from_record(json.loads(self.record_json)) if self.record_json else None


@dataclass(frozen=True)
class SkippedRow:
    source_id: str
    external_id: str
    company: str
    title: str
    location: str
    seen_at: datetime


@dataclass(frozen=True)
class ListRow:
    """A GitHub-list row someone collects, with the native posting's id when its link names one."""

    source_id: str
    external_id: str
    company: str
    title: str
    url: str
    alias: str | None
    posted_at: datetime | None
    seen_at: datetime


@dataclass(frozen=True)
class SourceState:
    source_id: str
    last_checked_at: datetime | None
    last_ok_at: datetime | None
    last_found_at: datetime | None
    consecutive_failures: int
    last_error: str | None
    open_count: int


@dataclass(frozen=True)
class DecisionRow:
    state: str
    rule_id: str | None
    settings_key: str
    digest: str | None


def time_or_none(value: Any) -> datetime | None:
    return parse_stamp(value) if isinstance(value, str) else None


def must_time(value: Any) -> datetime:
    parsed = time_or_none(value)
    if parsed is None:
        raise ValueError(f"expected a stored time, got {value!r}")
    return parsed

"""The records the engine works with: a listing as a board lists it, and a posting as the app stores it."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from typing import Any, Literal

SourceKind = Literal[
    "workday",
    "greenhouse",
    "ashby",
    "lever",
    "amazon",
    "apple",
    "google",
    "microsoft",
    "shopify",
    "uber",
    "netflix",
    "github_list",
    "jobbank",
    "linkedin",
]

# The old engine called the GitHub lists "community". Ids keep its spelling, so postings carry over.
_LEGACY_KIND = {"github_list": "community"}


def posting_id(kind: SourceKind, org: str, external_id_or_url: str) -> str:
    """The old engine's uid: the first 16 hex characters of sha1 over `source:org:raw_id-or-url`."""
    basis = f"{_LEGACY_KIND.get(kind, kind)}:{org}:{external_id_or_url}"
    return hashlib.sha1(basis.encode()).hexdigest()[:16]


def stamp(value: datetime | None) -> str | None:
    """RFC 3339 in UTC with a Z, or None."""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_stamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Listing:
    """One opening as a board lists it. `description=None` means its details still have to be fetched."""

    source_id: str
    kind: SourceKind
    org: str
    external_id: str
    company: str
    title: str
    location: str
    url: str
    posted_at: datetime | None
    terms: tuple[str, ...] = ()
    description: str | None = None
    alias: str | None = None


def listing_hash(listing: Listing) -> str:
    """What decides whether a known listing changed: its title, place, link, date and terms."""
    basis = [listing.title, listing.location, listing.url, stamp(listing.posted_at), list(listing.terms)]
    return hashlib.sha256(json.dumps(basis, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class Posting:
    """A collected posting, with exactly the fields of schema/posting.json."""

    id: str
    source_id: str
    external_id: str
    company: str
    title: str
    location: str
    url: str
    apply_url: str
    posted_at: datetime | None
    deadline: datetime | None
    remote: bool
    terms: tuple[str, ...]
    description: str
    first_seen_at: datetime
    closed_at: datetime | None
    content_hash: str

    def to_record(self) -> dict[str, Any]:
        return _record(asdict(self))

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Posting:
        values = dict(record)
        for name in _STAMPS:
            values[name] = parse_stamp(values[name])
        values["terms"] = tuple(values["terms"])
        return cls(**{f.name: values[f.name] for f in fields(cls)})


_STAMPS = ("posted_at", "deadline", "first_seen_at", "closed_at")
_NOT_CONTENT = ("id", "first_seen_at", "closed_at", "content_hash")


def _record(values: Mapping[str, Any]) -> dict[str, Any]:
    record = dict(values)
    for name in _STAMPS:
        record[name] = stamp(record[name])
    record["terms"] = list(record["terms"])
    return record


def content_hash(posting: Posting | Mapping[str, Any]) -> str:
    """sha256 over every field that is the posting's content.

    Not its id, when it was first seen, or when it closed.
    """
    values = asdict(posting) if isinstance(posting, Posting) else dict(posting)
    record = {k: v for k, v in _record(values).items() if k not in _NOT_CONTENT}
    canonical = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()

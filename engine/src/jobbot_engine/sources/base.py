"""What every reader shares: the interface, a job's details, the employer's timestamps, and the posting."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast, runtime_checkable

from dateutil import parser as dateparse

from jobbot_engine.model import Listing, Posting, SourceKind, content_hash, posting_id
from jobbot_engine.sources.http import Http, SourceError
from jobbot_engine.text import matches

# The ISO 8601 shapes Python reads exactly as dateutil does, without dateutil's cost.
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})?)?")
# Lever sends epoch milliseconds and others epoch seconds. Anything under 1e11 is seconds, unless jobs start
# being posted in the year 5138.
_SECONDS_BELOW = 1e11


@dataclass(frozen=True)
class Detail:
    """What a listing leaves out, fetched only for a posting that needs it."""

    description: str
    apply_url: str
    posted_at: datetime | None
    deadline: datetime | None
    location: str | None


class Openings(list[Listing]):
    """A reader's listings, and whether they're the whole board or only a window of it.

    A window (the newest few pages, or what changed lately) can't say a posting is gone, so nothing missing
    from one is closed by its absence.
    """

    def __init__(self, listings: Iterable[Listing] = (), complete: bool = True) -> None:
        super().__init__(listings)
        self.complete = complete


class Reader(Protocol):
    kind: SourceKind

    def openings(self, org: str, company: str, http: Http) -> list[Listing]: ...

    def detail(self, listing: Listing, http: Http) -> Detail: ...


# The most searches a board gets for its people's other roles, a run. With more words than this, each run
# takes a different few (scheduler.read.wants_of), so every word gets its turn.
SEARCHES = 6


@dataclass(frozen=True)
class Wants:
    """What the people using a board want searched for besides internships: words to search for, and the
    countries to search in (None: anywhere)."""

    queries: tuple[str, ...] = ()
    countries: frozenset[str] | None = None


@runtime_checkable
class Searches(Protocol):
    """A reader of a board that only answers searches, which can search for what its people want."""

    def search(self, org: str, company: str, http: Http, wants: Wants) -> Openings: ...


def from_listing(listing: Listing) -> Detail:
    """The detail of a listing that already carried everything its site publishes: no request needed."""
    return Detail(
        description=listing.description or "",
        apply_url=listing.url,
        posted_at=listing.posted_at,
        deadline=None,
        location=None,
    )


def timestamp(value: Any) -> datetime | None:
    """The employer's own timestamp, from text or an epoch number, in UTC. None when there isn't one."""
    if value in (None, "", 0):
        return None
    try:
        if isinstance(value, int | float):
            seconds = value if abs(value) < _SECONDS_BELOW else value / 1000
            return datetime.fromtimestamp(seconds, tz=UTC)
        raw = str(value)
        parsed = datetime.fromisoformat(raw) if _ISO.fullmatch(raw) else dateparse.parse(raw)
    except (ValueError, OverflowError, TypeError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


Json = dict[str, Any]


def answer_object(data: Any, what: str) -> Json:
    """A board's answer, if it's the JSON object the board usually answers with. Otherwise a plain error."""
    if not isinstance(data, dict):
        raise SourceError(f"{what} answered in a shape it doesn't usually have")
    return cast(Json, data)


def answer_list(data: Any, what: str) -> list[Json]:
    """A board's answer, if it's the JSON list the board usually answers with. Otherwise a plain error."""
    if not isinstance(data, list):
        raise SourceError(f"{what} answered in a shape it doesn't usually have")
    return objs(data)


def obj(value: Any) -> Json:
    """`value` as a JSON object, or an empty one."""
    return cast(Json, value) if isinstance(value, dict) else {}


def items(value: Any) -> list[Any]:
    """`value` as a JSON list, or an empty one."""
    return cast(list[Any], value) if isinstance(value, list) else []


def objs(value: Any) -> list[Json]:
    """The JSON objects in `value`, a list; anything else in it is left out."""
    return [cast(Json, item) for item in items(value) if isinstance(item, dict)]


def text(value: Any) -> str:
    """`value` as stripped text: a string, a number's digits, or empty."""
    return str(value).strip() if isinstance(value, str | int) else ""


def _cut(text: str, most: int) -> str:
    return text if len(text) <= most else text[: most - 1].rstrip() + "…"


def _terms(terms: tuple[str, ...]) -> tuple[str, ...]:
    """At most the 10 latest terms, each under 50 characters. Lists give terms oldest first."""
    kept = [term[:50] for term in terms if term.strip()]
    return tuple(kept[-10:])


def to_posting(listing: Listing, detail: Detail | None, first_seen_at: datetime) -> Posting:
    """The posting the app stores, cut to the contract's limits. A detail wins over what the listing said.

    Some list rows name dozens of cities, or every term from 2025 on; the app takes 300 characters of place
    and 10 terms, so the posting keeps the start of the one and the latest of the other.
    """
    location = _cut((detail.location if detail else None) or listing.location, 300)
    fields: dict[str, Any] = {
        "id": posting_id(listing.kind, listing.org, listing.external_id or listing.url),
        "source_id": listing.source_id,
        "external_id": _cut(listing.external_id or listing.url, 300),
        "company": _cut(listing.company, 200) or "Unknown company",
        "title": _cut(listing.title, 300) or "Untitled",
        "location": location,
        "url": listing.url,
        "apply_url": detail.apply_url if detail else listing.url,
        "posted_at": (detail.posted_at if detail else None) or listing.posted_at,
        "deadline": detail.deadline if detail else None,
        "remote": matches(["remote"], location) != [],
        "terms": _terms(listing.terms),
        "description": _cut(detail.description if detail else listing.description or "", 20000),
        "first_seen_at": first_seen_at,
        "closed_at": None,
    }
    return Posting(**fields, content_hash=content_hash(fields))

"""What a pasted link turns out to be, before it's added: which board, how many openings, how many match."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from jobbot_engine.model import stamp
from jobbot_engine.rules import Context, evaluate
from jobbot_engine.scheduler.read import wants_of
from jobbot_engine.sources import READERS, SourceError, to_posting
from jobbot_engine.sources.base import Searches, Wants
from jobbot_engine.sources.detect import board_url, from_page
from jobbot_engine.sources.github_list import LISTS
from jobbot_engine.sources.http import Http

UNREADABLE = "jobbot can't read this kind of page yet. It's kept as a request and shown on the Checked page."
SAMPLE = 5
# The rules a source check applies: what the title alone says.
_FROM_THE_TITLE = ("kind", "companies", "ruled_out", "roles")  # what a list of openings is enough to judge
_BOARDS = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workday": "Workday"}
# Boards that are a whole country's jobs, named as themselves.
_NAMED = {"jobbank": "Job Bank"}


def company_of(kind: str, org: str) -> str:
    """A readable name for a board's company, from its org."""
    if kind in _NAMED:
        return _NAMED[kind]
    if kind in READERS and kind not in _BOARDS and kind != "github_list":
        return kind.title()
    if kind == "github_list":
        return org.title() if org in LISTS else org.rsplit("/", 1)[-1]
    name = org.split("|")[0] if kind == "workday" else org
    return " ".join(word.capitalize() for word in name.replace("-", " ").replace("_", " ").split()) or org


def _what(kind: str, company: str) -> str:
    if kind in _NAMED:
        return company
    if kind in _BOARDS:
        return f"{company}'s {_BOARDS[kind]} board"
    if kind == "github_list":
        return f"The {company} list"
    return f"{company}'s careers site"


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _not_recognized(message: str) -> dict[str, Any]:
    return {
        "recognized": False,
        "source": None,
        "openings": 0,
        "matching": 0,
        "sample": [],
        "message": message,
    }


def run(
    job: Mapping[str, Any], http: Http, settings: Mapping[str, Any] | None, now: datetime | None = None
) -> dict[str, Any]:
    at = now or datetime.now(UTC)
    url: str = job["input"]["url"]
    try:
        found = from_page(url, http)
    except SourceError as error:
        return _not_recognized(f"jobbot couldn't open that link: {error}")
    if found is None:
        return _not_recognized(UNREADABLE)
    kind, org = found
    company = company_of(kind, org)
    try:
        reader = READERS[kind]
        wants = wants_of([{"settings": settings}] if settings is not None else [])
        # A sample: one of their role words, so Job Bank's waits between searches fit the check's time.
        wants = Wants(wants.queries[:1], wants.countries)
        if wants.queries and isinstance(reader, Searches):
            listings = reader.search(org, company, http, wants)
        else:
            listings = reader.openings(org, company, http)
    except SourceError as error:  # a board that's gone, or down: said plainly, not as a check gone wrong
        return _not_recognized(f"jobbot couldn't read that board: {error}")
    matching = [listing for listing in listings if _matches(to_posting(listing, None, at), settings, at)]
    sample = [
        {"title": listing.title[:300], "location": listing.location[:300], "url": listing.url}
        for listing in matching
        if listing.title and listing.url
    ][:SAMPLE]
    what = _what(kind, company)
    return {
        "recognized": True,
        "source": {
            "id": f"{kind}:{org}",
            "kind": kind,
            "org": org,
            "company": company,
            "url": board_url(kind, org, url),
            "added_at": stamp(at),
            "added_by": "person",
            "status": "unknown",
            "failing_since": None,
            "last_error": None,
        },
        "openings": len(listings),
        "matching": len(matching),
        "sample": sample,
        "message": f"{what} has {_count(len(listings), 'opening', 'openings')}; "
        f"{_count(len(matching), 'matches', 'match')} your rules.",
    }


def _matches(posting: Any, settings: Mapping[str, Any] | None, now: datetime) -> bool:
    if settings is None:
        return True
    context = Context(now=now, details=settings["details"], follows=())
    rules = {
        rule: {**chosen, "on": chosen["on"] and rule in _FROM_THE_TITLE}
        for rule, chosen in settings["rules"].items()
    }
    return evaluate(posting, {**settings, "rules": rules}, context).state == "new"

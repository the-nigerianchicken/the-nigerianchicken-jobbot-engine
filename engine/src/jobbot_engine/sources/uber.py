"""Uber: its careers site searches in the browser, so the list page serves the same ten roles whatever it is
asked. Its sitemap, though, is the whole board, each posting with the time it last changed. The postings that
changed recently are read one page each, for the date and text in their schema.org block. A posting's page
is half a megabyte, so this is a board to check hourly, not every five minutes.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from jobbot_engine.model import Listing, SourceKind
from jobbot_engine.sources.base import Detail, Json, Openings, from_listing, items, obj, text, timestamp
from jobbot_engine.sources.html import strip
from jobbot_engine.sources.http import Http, SourceError
from jobbot_engine.sources.pages import objects_with, payload

SITEMAP = "https://jobs.uber.com/en/jobs/sitemap.xml"
ENTRY = re.compile(r"<loc>(https://jobs\.uber\.com/en/jobs/(\d+)/)</loc>\s*<lastmod>([^<]+)</lastmod>")
LIMIT = 25
DAYS = 4


def _now() -> datetime:
    return datetime.now(UTC)


class Uber:
    kind: SourceKind = "uber"

    def __init__(self, clock: Callable[[], datetime] = _now) -> None:
        self.clock = clock

    def openings(self, org: str, company: str, http: Http) -> Openings:
        since = self.clock() - timedelta(days=DAYS)
        fresh: list[tuple[datetime, str, str]] = []
        for url, job_id, changed in ENTRY.findall(http.get_text(SITEMAP, what=f"{company}'s sitemap")):
            when = timestamp(changed)
            if when and when >= since:
                fresh.append((when, url, job_id))
        fresh.sort(reverse=True)
        found: list[Listing] = []
        for _when, url, job_id in fresh[:LIMIT]:
            try:
                page = http.get_text(url, what=f"{company}'s posting {job_id}")
            except SourceError:
                continue  # one posting's page failing leaves the others; it's read again next time
            posting = next(
                (j for j in objects_with(payload(page), "datePosted") if j.get("@type") == "JobPosting"), None
            )
            if posting is not None:
                found.append(_listing(org, company, url, job_id, posting))
        # The newest few of the board, not all of it: absence from it closes nothing.
        return Openings(found, complete=False)

    def detail(self, listing: Listing, http: Http) -> Detail:
        return from_listing(listing)


def _listing(org: str, company: str, url: str, job_id: str, posting: Json) -> Listing:
    where = posting.get("jobLocation")
    first = items(where)[0] if items(where) else where
    address = obj(obj(first).get("address"))
    place = (text(address.get(name)) for name in ("addressLocality", "addressRegion", "addressCountry"))
    return Listing(
        source_id=f"uber:{org}",
        kind="uber",
        org=org,
        external_id=job_id,
        company=company,
        title=" ".join(text(posting.get("title")).split()),
        location=", ".join(part for part in place if part),
        url=url,
        posted_at=timestamp(posting.get("datePosted")),
        description=strip(text(posting.get("description"))),
    )

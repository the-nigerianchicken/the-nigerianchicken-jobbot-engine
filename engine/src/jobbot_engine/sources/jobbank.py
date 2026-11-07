"""Job Bank, the Government of Canada's job board, through its Atom feed of a search's newest 100 jobs.

It holds every kind of job in Canada, so it's only searched: for internships, and for the roles its people
want besides (rules.searches). Its robots.txt asks for 5 seconds between requests, so they're WAIT seconds
apart, and it takes at most SEARCHES of those roles a run, so its waits fit a run's reading time. The feed
holds only the last couple of days' jobs (152 for "project" on 2026-11-06, over two pages), so the answer is
always a window: a job missing from it is older, not closed.
A feed entry has the job's title, link, date, place, employer and salary; the job's page isn't read.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ElementTree
from time import sleep
from urllib.parse import urlencode

from jobbot_engine.model import Listing, SourceKind
from jobbot_engine.places import countries_in
from jobbot_engine.sources.base import Detail, Openings, Wants, from_listing, timestamp
from jobbot_engine.sources.http import Http, SourceError
from jobbot_engine.sources.workday import QUERIES

FEED = "https://www.jobbank.gc.ca/jobsearch/feed/jobSearchRSSfeed"
ROWS = 100
WAIT = 6
SEARCHES = 3
_ATOM = "{http://www.w3.org/2005/Atom}"
_FIELD = re.compile(r"<strong>\s*([^<:]+?)\s*:\s*</strong>\s*([^<]*)")
_POSTING = re.compile(r"/jobposting/(\d+)")


class JobBank:
    kind: SourceKind = "jobbank"

    def openings(self, org: str, company: str, http: Http) -> Openings:
        return self.search(org, company, http, Wants())

    def search(self, org: str, company: str, http: Http, wants: Wants) -> Openings:
        found: dict[str, Listing] = {}
        for n, query in enumerate(dict.fromkeys([*QUERIES, *wants.queries[:SEARCHES]])):
            if n:
                sleep(WAIT)
            url = f"{FEED}?{urlencode({'searchstring': query, 'sort': 'D', 'rows': ROWS})}"
            what = f"Job Bank's search for {query}"
            try:
                root = ElementTree.fromstring(http.get_text(url, what=what))
            except ElementTree.ParseError as error:
                raise SourceError(f"{what} answered with a feed that isn't readable: {error}") from None
            for entry in root.findall(f"{_ATOM}entry"):
                listing = _listing(org, entry)
                if listing is not None:
                    found.setdefault(listing.external_id, listing)
        return Openings(list(found.values()), complete=False)

    def detail(self, listing: Listing, http: Http) -> Detail:
        return from_listing(listing)


def _listing(org: str, entry: ElementTree.Element) -> Listing | None:
    link = entry.find(f"{_ATOM}link")
    url = (link.get("href") or "").strip() if link is not None else ""
    number = _POSTING.search(url)
    title = (entry.findtext(f"{_ATOM}title") or "").strip()
    if number is None or not title:
        return None
    fields = {
        k.lower(): html.unescape(v).strip()
        for k, v in _FIELD.findall(entry.findtext(f"{_ATOM}summary") or "")
    }
    place = fields.get("location", "")
    if "Canada" not in countries_in(place):
        place = f"{place}, Canada" if place else "Canada"
    salary = fields.get("salary", "")
    return Listing(
        source_id=f"jobbank:{org}",
        kind="jobbank",
        org=org,
        external_id=number[1],
        company=fields.get("employer", "") or "An employer on Job Bank",
        title=html.unescape(title),
        location=place,
        url=url,
        posted_at=timestamp(entry.findtext(f"{_ATOM}updated")),
        description=f"Salary: {salary}" if salary else "",
    )

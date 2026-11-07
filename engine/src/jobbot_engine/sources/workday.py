"""Workday: about half of all listings.

The board's list gives only relative dates ("Posted 30+ Days Ago"), but each job's own endpoint carries the
employer's start date, so the list is searched for interns first and only a job's detail costs a second
request. The org is `tenant|host|site`, e.g. `nvidia|wd5.myworkdayjobs.com|NVIDIAExternalCareerSite`.

A board is also searched for the roles its people want besides internships, in their countries through the
board's own country filter, read from one more request for a single job of any kind: an intern search's
filter names only the countries it has interns in. A board with no job in those countries isn't searched for
them. A job only those searches found closes once no one searches for it any more.
"""

from __future__ import annotations

from typing import Any

from jobbot_engine import places
from jobbot_engine.model import Listing, SourceKind
from jobbot_engine.sources.base import (
    SEARCHES,
    Detail,
    Json,
    Openings,
    Wants,
    answer_object,
    items,
    obj,
    objs,
    text,
    timestamp,
)
from jobbot_engine.sources.html import strip
from jobbot_engine.sources.http import Http, SourceError

QUERIES = ("intern", "co-op")
PAGES = 2
PER_PAGE = 20


def parts(org: str) -> tuple[str, str]:
    """(front, api): the board's own address, which job links hang off, and the base of its JSON API.

    Two shapes are in the wild. Most tenants get a subdomain of their own
    (nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite); some share a host and are told apart by a path
    (wd1.myworkdaysite.com/recruiting/snapchat/snap). Writing the host as "wd1.myworkdaysite.com/recruiting"
    asks for the second.
    """
    pieces = org.split("|")
    if len(pieces) != 3:
        raise ValueError(f"a Workday org must be tenant|host|site, got {org!r}")
    tenant, host, site = pieces
    if "/" in host:
        host, prefix = host.split("/", 1)
        front, server = f"https://{host}/{prefix}/{tenant}/{site}", host
    else:
        front, server = f"https://{tenant}.{host}/{site}", f"{tenant}.{host}"
    # A tenant's name has underscores where its address has hyphens (osv_rubicon at osv-rubicon...), and the
    # API answers 422 to the address's spelling.
    return front, f"https://{server}/wday/cxs/{tenant.replace('-', '_')}/{site}"


class Workday:
    kind: SourceKind = "workday"

    def openings(self, org: str, company: str, http: Http) -> Openings:
        return self.search(org, company, http, Wants())

    def search(self, org: str, company: str, http: Http, wants: Wants) -> Openings:
        front, api = parts(org)
        what = f"{company}'s Workday board"
        found: list[Listing] = []
        seen: set[str] = set()
        whole = True

        def read(query: str, applied: Json) -> None:
            nonlocal whole
            for page in range(PAGES):
                payload: Json = {
                    "appliedFacets": applied,
                    "limit": PER_PAGE,
                    "offset": page * PER_PAGE,
                    "searchText": query,
                }
                data = answer_object(http.post_json(f"{api}/jobs", payload, what=what), what)
                jobs = objs(data.get("jobPostings"))
                for job in jobs:
                    path = text(job.get("externalPath"))
                    if not path or path in seen:
                        continue
                    seen.add(path)
                    found.append(_listing(org, company, front, path, job))
                if len(jobs) < PER_PAGE:
                    return
            whole = False  # every page full: the search found more than it was allowed to read

        for query in QUERIES:
            read(query, {})
        applied: Json | None = None
        if wants.queries and wants.countries is None:
            applied = {}
        elif wants.queries:
            probe: Json = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
            board = answer_object(http.post_json(f"{api}/jobs", probe, what=what), what)
            applied = _in_countries(board.get("facets"), wants.countries)
        if applied is not None:
            for query in wants.queries[:SEARCHES]:
                read(query, applied)
        return Openings(found, complete=whole)

    def detail(self, listing: Listing, http: Http) -> Detail:
        # Split on "/job/", not on the site name: the site name is often the tenant's name too, which also
        # appears in the host (copart.wd12.../copart/job/...).
        if "/job/" not in listing.url:
            raise SourceError(f"{listing.company}'s Workday job link has no /job/ path: {listing.url}")
        _front, api = parts(listing.org)
        what = f"{listing.company}'s Workday job {listing.external_id}"
        url = api + "/job/" + listing.url.split("/job/", 1)[1]
        info = obj(answer_object(http.get_json(url, what=what), what).get("jobPostingInfo"))
        return Detail(
            description=strip(text(info.get("jobDescription"))) or listing.title,
            apply_url=text(info.get("externalUrl")) or f"{listing.url}/apply",
            posted_at=timestamp(info.get("startDate")),
            deadline=None,
            location=text(info.get("location")) or None,
        )


def _in_countries(facets: Any, countries: frozenset[str] | None) -> Json | None:
    """The board's filter for these countries ({parameter: [ids]}), {} for no filter (anywhere, or the board
    has no country filter), or None when the board has no job in any of them."""
    if countries is None:
        return {}
    stack, filters = objs(facets), list[Json]()
    while stack:
        facet = stack.pop()
        for value in objs(facet.get("values")):
            if "values" in value:
                stack.append(value)  # a group of filters, one level down
        if "country" in text(facet.get("facetParameter")).lower():
            filters.append(facet)
    if not filters:
        return {}
    facet = filters[-1]
    ids = [
        text(v.get("id"))
        for v in objs(facet.get("values"))
        if places.country_named(text(v.get("descriptor"))) in countries and text(v.get("id"))
    ]
    return {text(facet.get("facetParameter")): ids} if ids else None


def _listing(org: str, company: str, front: str, path: str, job: Json) -> Listing:
    """A Workday job is named by its requisition number, the first of its bullet fields, or else its path."""
    bullets = items(job.get("bulletFields"))
    return Listing(
        source_id=f"workday:{org}",
        kind="workday",
        org=org,
        external_id=(text(bullets[0]) if bullets else "") or path,
        company=company,
        title=text(job.get("title")),
        location=text(job.get("locationsText")),
        url=f"{front}{path}",
        posted_at=None,
    )

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

import logging
import re
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
    in_turn,
    items,
    obj,
    objs,
    text,
    timestamp,
)
from jobbot_engine.sources.html import strip
from jobbot_engine.sources.http import Http, SourceError

log = logging.getLogger(__name__)

QUERIES = ("intern", "co-op")
# A filter value naming internships, as boards word them: "Apprentice & Intern", "Intern (Fixed Term)",
# "Co-op/Intern", "Student", "Apprenticeship (Fixed Term)". Read from the filters of all 502 boards followed
# on 2026-11-11; "Early Career" and "Graduate" were left out, as they hold new-graduate jobs.
INTERN_CATEGORY = re.compile(
    r"\b(?:interns?|internships?|co-?ops?|students?|apprentices?|apprenticeships?|trainees?)\b", re.I
)
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

        def read(query: str, applied: Json) -> Any:
            """Reads one search, and gives the filters its first page offered when it was cut off."""
            nonlocal whole
            facets: Any = None
            for page in range(PAGES):
                payload: Json = {
                    "appliedFacets": applied,
                    "limit": PER_PAGE,
                    "offset": page * PER_PAGE,
                    "searchText": query,
                }
                data = answer_object(http.post_json(f"{api}/jobs", payload, what=what), what)
                if page == 0:
                    facets = data.get("facets")
                jobs = objs(data.get("jobPostings"))
                for job in jobs:
                    path = text(job.get("externalPath"))
                    if not path or path in seen:
                        continue
                    seen.add(path)
                    found.append(_listing(org, company, front, path, job))
                if len(jobs) < PER_PAGE:
                    return None
            whole = False  # every page full: the search found more than it was allowed to read
            return facets

        # A search the board cut off may have buried its interns under jobs that only mention the word
        # (Cisco's "co-op" matched all 1,482 of its jobs), so the board's own intern category is read too.
        cut_off = [read(query, {}) for query in QUERIES]
        # A failure there keeps what the searches found (the board stays incomplete, so nothing closes).
        category = next(filter(None, map(_interns, cut_off)), None)
        if category:
            try:
                read("", category)
            except SourceError as error:
                log.warning("%s: its intern category wasn't read: %s", what, error)
        applied: Json | None = None
        if wants.queries and wants.countries is None:
            applied = {}
        elif wants.queries:
            probe: Json = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
            board = answer_object(http.post_json(f"{api}/jobs", probe, what=what), what)
            applied = _in_countries(board.get("facets"), wants.countries)
        roles = in_turn(wants.queries, wants.start)[:SEARCHES] if applied is not None else []
        for query in roles:
            read(query, applied or {})
        return Openings(found, complete=whole, taken=len(roles))

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


def _interns(facets: Any) -> Json | None:
    """The board's intern category ({parameter: [ids]}): the filter whose values named for internships hold
    the most jobs, or None when it has none."""
    best: tuple[int, Json] | None = None
    stack = objs(facets)
    while stack:
        facet = stack.pop()
        ids, jobs = list[str](), 0
        for value in objs(facet.get("values")):
            if "values" in value:
                stack.append(value)  # a group of filters, one level down
            elif INTERN_CATEGORY.search(text(value.get("descriptor"))) and text(value.get("id")):
                ids.append(text(value.get("id")))
                count = value.get("count")
                jobs += count if isinstance(count, int) else 0
        parameter = text(facet.get("facetParameter"))
        if ids and parameter and (best is None or jobs > best[0]):
            best = (jobs, {parameter: ids})
    return best[1] if best else None


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
    """A Workday job is named by its requisition number, or else its path."""
    return Listing(
        source_id=f"workday:{org}",
        kind="workday",
        org=org,
        external_id=_requisition(path, items(job.get("bulletFields"))) or path,
        company=company,
        title=text(job.get("title")),
        location=text(job.get("locationsText")),
        url=f"{front}{path}",
        posted_at=None,
    )


def _requisition(path: str, bullets: list[Any]) -> str:
    """The bullet field that ends the job's link: ..._JREQ203587, or ..._R0336211-1 for one in several places.

    Most boards put the requisition first, but 23 put a city, a label or "Posted 10 Days Ago" there, which
    jobs share or which changes daily; a job named by one took over another job's record (2026-11-12).
    """
    ends = (path, re.sub(r"-\d+$", "", path))
    for bullet in map(text, bullets):
        if bullet and any(end.endswith("_" + bullet) for end in ends):
            return bullet
    return ""

"""Netflix: left Workday for a careers site of its own, on Eightfold. The Workday board it left behind
answered every request with a 422 and never once returned a posting. The site's search API answers a server,
ten postings a page, with Netflix's own creation time and the whole description.
"""

from __future__ import annotations

from jobbot_engine.model import Listing, SourceKind
from jobbot_engine.sources.base import (
    Detail,
    Json,
    Openings,
    answer_object,
    from_listing,
    items,
    objs,
    text,
    timestamp,
)
from jobbot_engine.sources.html import strip
from jobbot_engine.sources.http import Http

API = "https://explore.jobs.netflix.net/api/apply/v2/jobs"
# "co-op" only ever matched descriptions, never a student role.
QUERIES = ("intern", "internship")
PAGES = 5
PER_PAGE = 10


def search_params(query: str, page: int) -> dict[str, str]:
    return {"domain": "netflix.com", "query": query, "start": str(page * PER_PAGE), "num": str(PER_PAGE)}


class Netflix:
    kind: SourceKind = "netflix"

    def openings(self, org: str, company: str, http: Http) -> Openings:
        what = f"{company}'s job search"
        found: list[Listing] = []
        seen: set[str] = set()
        for query in QUERIES:
            for page in range(PAGES):
                data = answer_object(http.get_json(API, search_params(query, page), what=what), what)
                positions = objs(data.get("positions"))
                for position in positions:
                    job_id = text(position.get("id")) or text(position.get("ats_job_id"))
                    if job_id and job_id not in seen:
                        seen.add(job_id)
                        found.append(_listing(org, company, job_id, position))
                count = data.get("count")
                if len(positions) < PER_PAGE or (page + 1) * PER_PAGE >= (
                    count if isinstance(count, int) else 0
                ):
                    break
        # The newest few of the board, not all of it: absence from it closes nothing.
        return Openings(found, complete=False)

    def detail(self, listing: Listing, http: Http) -> Detail:
        return from_listing(listing)


def _listing(org: str, company: str, job_id: str, position: Json) -> Listing:
    places = [text(place) for place in items(position.get("locations"))] or [text(position.get("location"))]
    name = text(position.get("name"))
    return Listing(
        source_id=f"netflix:{org}",
        kind="netflix",
        org=org,
        external_id=job_id,
        company=company,
        title=name,
        location="; ".join(place for place in places if place)[:200],
        url=text(position.get("canonicalPositionUrl"))
        or f"https://explore.jobs.netflix.net/careers/job/{job_id}",
        posted_at=timestamp(position.get("t_create")),
        description=strip(text(position.get("job_description"))) or name,
    )

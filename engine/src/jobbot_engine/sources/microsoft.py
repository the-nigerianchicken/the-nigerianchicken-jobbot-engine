"""Microsoft: its careers site runs on Phenom, whose search endpoint is public JSON with the employer's own
posted time. One query per kind of student role; asking for everything would be 9,000 postings a run.
"""

from __future__ import annotations

from urllib.parse import urljoin

from jobbot_engine.model import Listing, SourceKind
from jobbot_engine.sources.base import (
    Detail,
    Json,
    Openings,
    answer_object,
    from_listing,
    items,
    obj,
    objs,
    text,
    timestamp,
)
from jobbot_engine.sources.http import Http

SITE = "https://apply.careers.microsoft.com/"
API = f"{SITE}api/pcsx/search"
QUERIES = (
    "software engineer intern",
    "software engineering internship",
    "research intern",
    "data scientist intern",
    "explore intern",
)
PER_QUERY = 40


def search_params(query: str) -> dict[str, str]:
    return {
        "domain": "microsoft.com",
        "query": query,
        "location": "",
        "start": "0",
        "num": str(PER_QUERY),
        "sort_by": "date",
    }


class Microsoft:
    kind: SourceKind = "microsoft"

    def openings(self, org: str, company: str, http: Http) -> Openings:
        what = f"{company}'s job search"
        found: list[Listing] = []
        seen: set[str] = set()
        for query in QUERIES:
            data = answer_object(http.get_json(API, search_params(query), what=what), what)
            for position in objs(obj(data.get("data")).get("positions")):
                job_id = next(
                    (text(position.get(k)) for k in ("atsJobId", "displayJobId", "id") if position.get(k)), ""
                )
                if job_id and job_id not in seen:
                    seen.add(job_id)
                    found.append(_listing(org, company, job_id, position))
        # The newest few of the board, not all of it: absence from it closes nothing.
        return Openings(found, complete=False)

    def detail(self, listing: Listing, http: Http) -> Detail:
        return from_listing(listing)


def _listing(org: str, company: str, job_id: str, position: Json) -> Listing:
    about = (text(position.get(name)) for name in ("name", "department", "workLocationOption"))
    return Listing(
        source_id=f"microsoft:{org}",
        kind="microsoft",
        org=org,
        external_id=job_id,
        company=company,
        title=text(position.get("name")),
        location="; ".join(text(place) for place in items(position.get("locations")))[:200],
        url=urljoin(SITE, text(position.get("positionUrl")) or f"careers/job/{job_id}"),
        posted_at=timestamp(position.get("postedTs")) or timestamp(position.get("creationTs")),
        description=" ".join(part for part in about if part),
    )

"""Amazon runs its own board. amazon.jobs has a public search with the employer's own posted date, and sorted
by recent, a few pages cover everything posted in the last week or so. Applying still needs an Amazon account.
"""

from __future__ import annotations

from jobbot_engine.model import Listing, SourceKind
from jobbot_engine.sources.base import (
    Detail,
    Json,
    Openings,
    answer_object,
    from_listing,
    objs,
    text,
    timestamp,
)
from jobbot_engine.sources.html import strip
from jobbot_engine.sources.http import Http

SITE = "https://www.amazon.jobs"
SEARCH = f"{SITE}/en/search.json"
PAGES = 3
PER_PAGE = 100


def search_params(page: int) -> dict[str, str]:
    return {
        "category[]": "software-development",
        "sort": "recent",
        "result_limit": str(PER_PAGE),
        "offset": str(page * PER_PAGE),
    }


class Amazon:
    kind: SourceKind = "amazon"

    def openings(self, org: str, company: str, http: Http) -> Openings:
        what = f"{company}'s job search"
        found: list[Listing] = []
        seen: set[str] = set()
        for page in range(PAGES):
            jobs = objs(
                answer_object(http.get_json(SEARCH, search_params(page), what=what), what).get("jobs")
            )
            for job in jobs:
                job_id = text(job.get("id_icims")) or text(job.get("id"))
                if job_id and job_id not in seen:
                    seen.add(job_id)
                    found.append(_listing(org, company, job_id, job))
            if len(jobs) < PER_PAGE:
                break
        # The newest few of the board, not all of it: absence from it closes nothing.
        return Openings(found, complete=False)

    def detail(self, listing: Listing, http: Http) -> Detail:
        return from_listing(listing)


def _listing(org: str, company: str, job_id: str, job: Json) -> Listing:
    place = (text(job.get(name)) for name in ("city", "state", "country_code"))
    about = (text(job.get(name)) for name in ("description_short", "description", "basic_qualifications"))
    return Listing(
        source_id=f"amazon:{org}",
        kind="amazon",
        org=org,
        external_id=job_id,
        company=text(job.get("company_name")) or company,
        title=text(job.get("title")),
        location=text(job.get("location")) or ", ".join(part for part in place if part),
        url=SITE + text(job.get("job_path")),
        posted_at=timestamp(job.get("posted_date")),
        description=strip(
            " ".join(part for part in [*about, text(job.get("preferred_qualifications"))] if part)
        ),
    )

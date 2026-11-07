"""Apple: the search page carries every posting on it as JSON, summary and all, with the post date to the
second. Apple files every student role under one team, which is a better filter than any search words.
"""

from __future__ import annotations

from urllib.parse import urlencode

from jobbot_engine.model import Listing, SourceKind
from jobbot_engine.sources.base import Detail, Json, Openings, from_listing, obj, objs, text, timestamp
from jobbot_engine.sources.html import strip
from jobbot_engine.sources.http import Http
from jobbot_engine.sources.pages import objects_with, payload

SEARCH = "https://jobs.apple.com/en-us/search"
FILTERS = ({"team": "internships-STDNT-INTRN"}, {"key": "software engineering internship"})
PAGES = 2


def search_url(search: dict[str, str], page: int) -> str:
    return f"{SEARCH}?{urlencode({**search, 'sort': 'newest', 'page': str(page)})}"


class Apple:
    kind: SourceKind = "apple"

    def openings(self, org: str, company: str, http: Http) -> Openings:
        what = f"{company}'s job search"
        found: list[Listing] = []
        seen: set[str] = set()
        for search in FILTERS:
            for page in range(1, PAGES + 1):
                jobs = objects_with(
                    payload(http.get_text(search_url(search, page), what=what)), "postDateInGMT"
                )
                if not jobs:
                    break
                for job in jobs:
                    job_id = text(job.get("positionId"))
                    if job_id and job_id not in seen:
                        seen.add(job_id)
                        found.append(_listing(org, company, job_id, job))
        # The newest few of the board, not all of it: absence from it closes nothing.
        return Openings(found, complete=False)

    def detail(self, listing: Listing, http: Http) -> Detail:
        return from_listing(listing)


def _place(location: Json) -> str:
    city = (text(location.get(name)) for name in ("city", "stateProvince"))
    return text(location.get("name")) or ", ".join(part for part in city if part)


def _listing(org: str, company: str, job_id: str, job: Json) -> Listing:
    slug = text(job.get("transformedPostingTitle")) or "role"
    team = text(obj(job.get("team")).get("teamCode"))
    places = (_place(location) for location in objs(job.get("locations")))
    return Listing(
        source_id=f"apple:{org}",
        kind="apple",
        org=org,
        external_id=job_id,
        company=company,
        title=text(job.get("postingTitle")),
        location="; ".join(place for place in places if place)[:200],
        url=f"https://jobs.apple.com/en-us/details/{job_id}/{slug}" + (f"?team={team}" if team else ""),
        posted_at=timestamp(job.get("postDateInGMT")) or timestamp(job.get("postingDate")),
        description=strip(text(job.get("jobSummary")) or text(job.get("description"))),
    )

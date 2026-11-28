"""Greenhouse: listed without content, which is most of the answer's size; a job's content only on request.

`first_published` is the post date, unless the job was reused: a company that reopens last year's posting
for a new season keeps its first publish date, so a job updated half a year or more after it was first
published is dated by that update. A shorter gap is an ordinary edit, which says nothing about its age.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from jobbot_engine.model import Listing, SourceKind
from jobbot_engine.sources.base import Detail, Json, answer_object, obj, objs, text, timestamp
from jobbot_engine.sources.html import strip
from jobbot_engine.sources.http import Http

API = "https://boards-api.greenhouse.io/v1/boards"
REUSED = timedelta(days=180)  # an internship season: a gap this long is a posting reopened, not an edit


def _posted(job: Json) -> datetime | None:
    published, updated = timestamp(job.get("first_published")), timestamp(job.get("updated_at"))
    if published is not None and updated is not None and updated - published >= REUSED:
        return updated
    return published or updated


class Greenhouse:
    kind: SourceKind = "greenhouse"

    def openings(self, org: str, company: str, http: Http) -> list[Listing]:
        what = f"{company}'s Greenhouse board"
        data = answer_object(http.get_json(f"{API}/{org}/jobs", what=what), what)
        return [
            Listing(
                source_id=f"greenhouse:{org}",
                kind="greenhouse",
                org=org,
                external_id=text(job.get("id")),
                company=company,
                title=text(job.get("title")),
                location=text(obj(job.get("location")).get("name")),
                url=text(job.get("absolute_url")),
                posted_at=_posted(job),
            )
            for job in objs(data.get("jobs"))
        ]

    def detail(self, listing: Listing, http: Http) -> Detail:
        what = f"{listing.company}'s Greenhouse job {listing.external_id}"
        url = f"{API}/{listing.org}/jobs/{listing.external_id}"
        job = answer_object(http.get_json(url, what=what), what)
        return Detail(
            description=strip(text(job.get("content"))),
            apply_url=text(job.get("absolute_url")) or listing.url,
            posted_at=_posted(job),
            deadline=timestamp(job.get("application_deadline")),
            location=text(obj(job.get("location")).get("name")) or None,
        )

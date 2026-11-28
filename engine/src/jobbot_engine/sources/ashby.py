"""Ashby: the description is in the listing; the detail is the application deadline.

Ashby's public board feed omits deadlines, so a posting whose deadline passed in May can be re-published
today and look brand new. The board page's own GraphQL has the field, and it's asked one job at a time.
"""

from __future__ import annotations

from jobbot_engine.model import Listing, SourceKind
from jobbot_engine.sources.base import Detail, Json, answer_object, obj, objs, text, timestamp
from jobbot_engine.sources.http import Http

API = "https://api.ashbyhq.com/posting-api/job-board"
# A big company's board, every posting with its description, outgrows the usual 10 MB: OpenAI's was 13.5 MB
# on 2026-10-12, and was refused every run until this.
BOARD_CAP = 32 * 1024 * 1024
DEADLINE_URL = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
DEADLINE_QUERY = (
    "query ApiJobPosting($o: String!, $j: String!) { jobPosting("
    "organizationHostedJobsPageName: $o, jobPostingId: $j) { applicationDeadline } }"
)


class Ashby:
    kind: SourceKind = "ashby"

    def openings(self, org: str, company: str, http: Http) -> list[Listing]:
        what = f"{company}'s Ashby board"
        data = answer_object(http.get_json(f"{API}/{org}", what=what, cap=BOARD_CAP), what)
        return [
            Listing(
                source_id=f"ashby:{org}",
                kind="ashby",
                org=org,
                external_id=text(job.get("id")),
                company=company,
                title=text(job.get("title")),
                location=text(job.get("location")),
                url=text(job.get("jobUrl")),
                posted_at=timestamp(job.get("publishedAt")),
                description=text(job.get("descriptionPlain")),
            )
            for job in objs(data.get("jobs"))
            if job.get("isListed") is not False
        ]

    def detail(self, listing: Listing, http: Http) -> Detail:
        what = f"{listing.company}'s Ashby job {listing.external_id}"
        payload: Json = {
            "operationName": "ApiJobPosting",
            "query": DEADLINE_QUERY,
            "variables": {"o": listing.org, "j": listing.external_id},
        }
        data = answer_object(http.post_json(DEADLINE_URL, payload, what=what), what)
        job = obj(obj(data.get("data")).get("jobPosting"))
        # Ashby's apply link is always the job's link with /application after it.
        return Detail(
            description=listing.description or "",
            apply_url=f"{listing.url}/application",
            posted_at=listing.posted_at,
            deadline=timestamp(job.get("applicationDeadline")),
            location=None,
        )

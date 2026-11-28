"""Lever: the whole description is in the listing, so a job's detail needs no request."""

from __future__ import annotations

from jobbot_engine.model import Listing, SourceKind
from jobbot_engine.sources.base import Detail, Json, answer_list, obj, objs, text, timestamp
from jobbot_engine.sources.html import strip
from jobbot_engine.sources.http import Http

API = "https://api.lever.co/v0/postings"


def _description(job: Json) -> str:
    """The opening, then each list under its heading, then the closing text.

    The lists are where a posting's requirements usually are, so they're part of what the rules read.
    """
    parts = [text(job.get("descriptionPlain")) or strip(text(job.get("description")))]
    for section in objs(job.get("lists")):
        parts.append(f"{text(section.get('text'))}\n{strip(text(section.get('content')))}")
    parts.append(text(job.get("additionalPlain")))
    return "\n\n".join(part.strip() for part in parts if part.strip())


class Lever:
    kind: SourceKind = "lever"

    def openings(self, org: str, company: str, http: Http) -> list[Listing]:
        what = f"{company}'s Lever board"
        jobs = answer_list(http.get_json(f"{API}/{org}", {"mode": "json"}, what=what), what)
        return [
            Listing(
                source_id=f"lever:{org}",
                kind="lever",
                org=org,
                external_id=text(job.get("id")),
                company=company,
                title=text(job.get("text")),
                location=text(obj(job.get("categories")).get("location")) or text(job.get("country")),
                url=text(job.get("hostedUrl")),
                posted_at=timestamp(job.get("createdAt")),
                description=_description(job),
            )
            for job in jobs
        ]

    def detail(self, listing: Listing, http: Http) -> Detail:
        # Lever's apply link is always the job's link with /apply after it.
        return Detail(
            description=listing.description or "",
            apply_url=f"{listing.url}/apply",
            posted_at=listing.posted_at,
            deadline=None,
            location=None,
        )

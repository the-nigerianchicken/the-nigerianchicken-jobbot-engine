"""Shopify: its board runs on a rented Ashby that isn't published under a public board name, so the careers
page itself is the feed. Title and link only: the page publishes no date, and the posting's own page keeps
its text in an encoding that changes with every deploy. The page ignores a search word and serves the whole
board either way, so it's asked once and the person's own rules do the filtering.
"""

from __future__ import annotations

import re

from jobbot_engine.model import Listing, SourceKind
from jobbot_engine.sources.base import Detail, from_listing
from jobbot_engine.sources.http import Http

SEARCH = "https://www.shopify.com/careers/search"
LINK = re.compile(r"/careers/([a-z0-9-]+)_([0-9a-f-]{36})")

_SMALL = {"and", "or", "of", "for", "the", "to", "in", "at", "on", "a", "an", "with"}
_CAPS = {
    "ai": "AI",
    "ml": "ML",
    "ios": "iOS",
    "ux": "UX",
    "ui": "UI",
    "api": "API",
    "sre": "SRE",
    "qa": "QA",
    "us": "US",
    "uk": "UK",
    "emea": "EMEA",
    "apac": "APAC",
    "nlp": "NLP",
    "llm": "LLM",
    "sde": "SDE",
    "it": "IT",
    "hr": "HR",
}


def title_from_slug(slug: str) -> str:
    """The title a link's slug spells, since the page gives no other."""
    titled: list[str] = []
    for i, word in enumerate(slug.split("-")):
        if word in _CAPS:
            titled.append(_CAPS[word])
        elif i and word in _SMALL:
            titled.append(word)
        else:
            titled.append(word.capitalize())
    return " ".join(titled)


class Shopify:
    kind: SourceKind = "shopify"

    def openings(self, org: str, company: str, http: Http) -> list[Listing]:
        found: list[Listing] = []
        seen: set[str] = set()
        for slug, job_id in LINK.findall(http.get_text(SEARCH, what=f"{company}'s careers page")):
            if job_id in seen:
                continue
            seen.add(job_id)
            found.append(
                Listing(
                    source_id=f"shopify:{org}",
                    kind="shopify",
                    org=org,
                    external_id=job_id,
                    company=company,
                    title=title_from_slug(slug),
                    location="",
                    url=f"https://www.shopify.com/careers/{slug}_{job_id}",
                    posted_at=None,
                    description="",
                )
            )
        return found

    def detail(self, listing: Listing, http: Http) -> Detail:
        return from_listing(listing)

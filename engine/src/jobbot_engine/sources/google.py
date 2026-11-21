"""Google: the results page is rendered on the server, and its own filter for student roles (target_level) is
exactly the set wanted. No posted date is published anywhere on it, so these arrive dateless.
"""

from __future__ import annotations

import html as htmlmod
import re
from urllib.parse import urlencode, urljoin

from jobbot_engine.model import Listing, SourceKind
from jobbot_engine.sources.base import Detail, Openings, from_listing
from jobbot_engine.sources.html import strip
from jobbot_engine.sources.http import Http
from jobbot_engine.sources.pages import words

ROOT = "https://www.google.com/about/careers/applications/"
PAGES = 2
FULL_PAGE = 20
# Each job sits in a card of its own; a card starts with this.
CARD_START = '<li class="lLd3Je"'
_CARD = re.compile(
    r'<a class="[^"]*"\s+href="(jobs/results/(\d+)-[^"?]*[^"]*)"\s+aria-label="Learn more about ([^"]*)"',
    re.S,
)


def results_url(page: int) -> str:
    params = {"target_level": "INTERN_AND_APPRENTICE", "sort_by": "date", "page": str(page)}
    return f"{ROOT}jobs/results/?{urlencode(params)}"


def cards(html: str) -> list[tuple[str, str, str]]:
    """(link, job id, title) for each job card on a results page."""
    return [(href, job_id, title) for href, job_id, title in _CARD.findall(html)]


class Google:
    kind: SourceKind = "google"

    def openings(self, org: str, company: str, http: Http) -> Openings:
        what = f"{company}'s careers site"
        found: list[Listing] = []
        seen: set[str] = set()
        for page in range(1, PAGES + 1):
            html = http.get_text(results_url(page), what=what)
            on_page = cards(html)
            for href, job_id, title in on_page:
                if job_id in seen:
                    continue
                seen.add(job_id)
                found.append(
                    Listing(
                        source_id=f"google:{org}",
                        kind="google",
                        org=org,
                        external_id=job_id,
                        company=company,
                        title=htmlmod.unescape(words(title)),
                        location=card_place(html, job_id),
                        url=urljoin(ROOT, href.replace("&amp;", "&")),
                        posted_at=None,
                        description=card_about(html, job_id),
                    )
                )
            if len(on_page) < FULL_PAGE:
                break
        # The newest few of the board, not all of it: absence from it closes nothing.
        return Openings(found, complete=False)

    def detail(self, listing: Listing, http: Http) -> Detail:
        return from_listing(listing)


def _card(html: str, job_id: str) -> str:
    """The card a job id sits in: from the card boundary before it to the next."""
    at = html.find("-" + job_id + "-")
    at = html.find(job_id) if at < 0 else at
    if at < 0:
        return ""
    start = html.rfind(CARD_START, 0, at)
    if start < 0:
        start = max(0, at - 6000)
    end = html.find(CARD_START, at)
    return html[start : end if end > 0 else at + 2000]


def card_place(html: str, job_id: str) -> str:
    """Up to three places, each once, and how many more the card mentions."""
    card = _card(html, job_id)
    places: list[str] = []
    for raw in re.findall(r'<span class="r0wTof[^"]*">([^<]+)</span>', card):
        place = htmlmod.unescape(raw).strip().lstrip(";").strip()
        if place and place.lower() not in (seen.lower() for seen in places):
            places.append(place)
    where = "; ".join(places[:3])
    more = re.search(r'<span class="BVHzed">;\s*\+(\d+) more</span>', card)
    if more and where:
        where += f"; and {more.group(1)} more"
    return where


def card_about(html: str, job_id: str) -> str:
    """The card's qualifications, which are the only description the results page has."""
    about = re.search(r'<div class="Xsxa1e">(.*?)</div>', _card(html, job_id), re.S)
    return strip(about.group(1)) if about else ""

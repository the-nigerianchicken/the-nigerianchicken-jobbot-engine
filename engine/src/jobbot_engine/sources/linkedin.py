"""LinkedIn's jobs, through the public job search anyone sees without signing in.

Most business jobs (project, product, marketing, finance) are posted there and nowhere jobbot reads: on
2026-11-07, 20 entry-level ones in Canada from a week came from 15 employers, none of them a source. It holds
every job, so it's only searched: for each person's role words, whatever their kind of job, in each of their
countries, each with a word of a kind they chose ("entry level marketing"). Its search ignores its own
Experience level filter: on 2026-11-08 "marketing" in Canada answered the same senior jobs with it and
without, and an entry-level person kept 5 of 40; as "entry level marketing" they kept 34 of 40. Its terms
don't welcome automated reading, so it's read lightly: WAIT seconds between requests and at most REQUESTS
a run. Each search (a word in a country) gets its turn: a run takes the next ones after those it answered
before, a first page of each before any second page. Taken at random, one of 120 waited four days
(2026-11-12). When it turns requests away, what was read already is kept, and the searches it didn't answer
come first next run.
A search's answer is the newest jobs of the last week, so it's always a window: a job missing from it isn't
closed. A result has the job's title, employer, place and date; its page isn't read.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from time import sleep
from urllib.parse import urlencode

from jobbot_engine.model import Listing, SourceKind
from jobbot_engine.sources.base import Detail, Openings, RoleSearch, Wants, from_listing, in_turn
from jobbot_engine.sources.http import Http, TurnedAway
from jobbot_engine.sources.workday import QUERIES

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
WAIT = 6
REQUESTS = 6
PER_PAGE = 10  # a search answers ten at a time: a shorter page is the last
PAGES = 2
WEEK = "r604800"
# The word each kind of job puts in a search. New grad and Entry level search the same: LinkedIn's new-grad
# jobs say entry level. With Kind of job off ("Any"), a role word is searched alone.
KIND_WORDS = {
    "Internship": "intern",
    "Co-op": "co-op",
    "Apprenticeship": "apprentice",
    "New grad": "entry level",
    "Entry level": "entry level",
    "Part-time": "part time",
    "Contract": "contract",
}

_CARD = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"(.*?)(?=data-entity-urn=|\Z)', re.S)
_TITLE = re.compile(r'class="base-search-card__title"[^>]*>(.*?)</h3>', re.S)
_COMPANY = re.compile(r'class="base-search-card__subtitle"[^>]*>(.*?)</h4>', re.S)
_PLACE = re.compile(r'class="job-search-card__location"[^>]*>(.*?)</span>', re.S)
_DATE = re.compile(r'<time[^>]*datetime="(\d{4}-\d{2}-\d{2})"')
_TAG = re.compile(r"<[^>]+>")


class LinkedIn:
    kind: SourceKind = "linkedin"
    by_role = True  # searched for its people's roles whatever their kind of job

    def openings(self, org: str, company: str, http: Http) -> Openings:
        return self.search(org, company, http, Wants())

    def search(self, org: str, company: str, http: Http, wants: Wants) -> Openings:
        pending = in_turn(_searches(wants), wants.start)
        found: dict[str, Listing] = {}
        asked = answered = 0  # requests made, and searches whose first page was read
        for n in range(PAGES):
            full: list[tuple[str, str]] = []
            for word, place in pending:
                if asked >= REQUESTS:
                    return Openings(list(found.values()), complete=False, taken=answered)
                if asked:
                    sleep(WAIT)
                asked += 1
                url = f"{SEARCH}?{urlencode(_params(word, place, n * PER_PAGE))}"
                try:
                    text = http.get_text(url, what=f"LinkedIn's search for {word} in {place}")
                except TurnedAway:
                    if not answered:
                        raise
                    return Openings(list(found.values()), complete=False, taken=answered)  # keep what it read
                answered += n == 0
                cards = [_listing(org, number, card) for number, card in _CARD.findall(text)]
                for listing in cards:
                    if listing is not None:
                        found.setdefault(listing.external_id, listing)
                if len(cards) == PER_PAGE:
                    full.append((word, place))
            pending = full
        return Openings(list(found.values()), complete=False, taken=answered)

    def detail(self, listing: Listing, http: Http) -> Detail:
        return from_listing(listing)


def _searches(wants: Wants) -> list[tuple[str, str]]:
    """Every search (words, country) its people want, each person's role words with their own kinds in their
    own countries, each once, in a fixed order; internships worldwide when no one has a role."""
    groups = wants.groups or (RoleSearch(wants.roles, wants.levels, wants.where),)
    pending: dict[tuple[str, str], None] = {}
    for group in groups:
        words = [f"{kind} {role}".strip() for role in group.roles for kind in _kind_words(group.levels)]
        places = sorted(group.where) if group.where else ["Worldwide"]
        pending.update(((word, place), None) for word in words for place in places)
    return list(pending) or [(word, "Worldwide") for word in QUERIES]


def _kind_words(kinds: frozenset[str]) -> list[str]:
    """The words the chosen kinds put in a search, in KIND_WORDS' order, and "" (the role word alone) for
    someone with Kind of job off ("Any") or when no kind is named. Kinds are everyone's who reads it together,
    so one person's Kind of job off doesn't take the others' kinds out."""
    words = [word for kind, word in KIND_WORDS.items() if kind in kinds]
    return list(dict.fromkeys([*words, *([""] if "Any" in kinds or not words else [])]))


def _params(word: str, place: str, start: int) -> dict[str, str]:
    return {"keywords": word, "location": place, "f_TPR": WEEK, "sortBy": "DD", "start": str(start)}


def _text(found: re.Match[str] | None) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", found[1])).split()) if found else ""


def _listing(org: str, number: str, card: str) -> Listing | None:
    title = _text(_TITLE.search(card))
    if not title:
        return None
    date = _DATE.search(card)
    return Listing(
        source_id=f"linkedin:{org}",
        kind="linkedin",
        org=org,
        external_id=number,
        company=_text(_COMPANY.search(card)) or "An employer on LinkedIn",
        title=title,
        location=_text(_PLACE.search(card)),
        url=f"https://www.linkedin.com/jobs/view/{number}",
        posted_at=datetime.fromisoformat(date[1]).replace(tzinfo=UTC) if date else None,
        description="",
    )

"""The GitHub internship lists, read as a source of postings.

Most internships are posted on boards jobbot doesn't read itself: Oracle, iCIMS, SmartRecruiters, a company's
own site. The SimplifyJobs and vanshb03 lists track those by hand, across every site, usually within hours,
so every row on them is a posting. Where the same job is on a board jobbot reads itself, the row carries the
native posting's id as its alias, and the native copy, which has the full description, wins.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse

from jobbot_engine.model import Listing, SourceKind
from jobbot_engine.sources.base import Detail, Json, answer_list, from_listing, items, text, timestamp
from jobbot_engine.sources.detect import native_id
from jobbot_engine.sources.http import Http, SourceError

LISTS = {
    "simplify": "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/"
    ".github/scripts/listings.json",
    "vansh": "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json",
}
# A list named by its repository, owner/repo, is read from the same place in it as the two above.
_BY_REPOSITORY = "https://raw.githubusercontent.com/{}/dev/.github/scripts/listings.json"
# Hosts of the short links a list gives in place of a job's own link (every vansh row's). Where one leads is
# asked once (Http.location), so the row is known for the job it is: on its board, and on the other lists.
SHORT_LINK_HOSTS = frozenset({"go.vansh.nyc"})

# The Simplify list was 12.7 MB on 2026-10-03, 37 MB once read, and grows through the season. A board's 10 MB
# cap would refuse it, so a list may be up to 40 MB. The lists are read after the boards, not alongside them.
LIST_CAP = 40 * 1024 * 1024

# What the lists record about sponsorship, and which of those rule a seat out. Only these reach the
# description, so that "Offers Sponsorship" is never read by a rule as a mention of sponsorship.
BLOCKS = {"Does Not Offer Sponsorship", "U.S. Citizenship is Required"}


def list_url(org: str) -> str:
    if org in LISTS:
        return LISTS[org]
    if re.fullmatch(r"[\w.-]+/[\w.-]+", org):
        return _BY_REPOSITORY.format(org)
    raise SourceError(f"There's no GitHub list called {org}")


def is_short(url: str) -> bool:
    return urlparse(url).netloc.lower() in SHORT_LINK_HOSTS


def twin_key(company: str, title: str) -> str:
    """What a list row and a native posting share when they're the same job: company and title, letters only.

    The last resort when a row names no posting and its link doesn't match one (link_key).
    """

    def key(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.lower())

    return f"{key(company)}|{key(title)}"


def link_key(url: str) -> str | None:
    """What a list row's link and a native posting's link share when they're the same job, or None.

    The same job's link differs between a list and the board in ways that don't change the job: tracking
    parameters, letter case, a trailing slash, and on Workday the site name, a language part and a suffix on
    the requisition number (`/private/job/.../Mobile-Engineer_R-110306` is `/search/job/..._R-110306-1`).
    So a Workday link is known by its company and requisition number; any other by the rest of its address.
    """
    parsed = urlparse((url or "").strip())
    host = parsed.netloc.lower().removeprefix("www.")
    if not host:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if host.endswith((".myworkdayjobs.com", ".myworkdaysite.com")):
        if host.endswith(".myworkdaysite.com"):
            tenant = parts[1].lower() if len(parts) > 1 and parts[0] == "recruiting" else None
        else:
            tenant = host.split(".")[0]
        # A bare wd5.myworkdayjobs.com link doesn't say whose job it is.
        if not tenant or re.fullmatch(r"wd\d+", tenant) or "job" not in parts or "_" not in parts[-1]:
            return None
        # A repost adds "-1" to a long number: R-110306-1 is R-110306, but JR-2026-07 isn't JR-2026-08.
        requisition = re.sub(r"(?<=\d{5})-\d{1,2}$", "", parts[-1].rsplit("_", 1)[1].lower())
        return f"workday|{tenant}|{requisition}"
    kept = sorted((k, v) for k, v in parse_qsl(parsed.query) if not k.lower().startswith("utm_"))
    return f"{host}/{'/'.join(parts).lower()}?{urlencode(kept)}"


def job_link(url: str) -> str | None:
    """A link's link_key, if the link names one job: its path or query carries a job's number (3 digits or
    more, and not a year) or id (8 letters and digits or more, as in a UUID). A careers page many jobs share
    names none, even with a year in it (`/students/2027-internships`) or a digit in its host."""
    key = link_key(url)
    if key is None:
        return None
    if key.startswith("workday|"):
        return key
    parsed = urlparse(url.strip())
    words = re.split(
        r"[^a-z0-9]+", f"{parsed.path} {' '.join(v for _, v in parse_qsl(parsed.query))}".lower()
    )
    return key if any(_names_a_job(word) for word in words) else None


def _names_a_job(word: str) -> bool:
    if word.isdigit():
        return len(word) >= 3 and not re.fullmatch(r"(19|20)\d\d", word)
    return len(word) >= 8 and any(c.isdigit() for c in word) and re.fullmatch(r"[0-9a-f]+", word) is not None


class GithubList:
    kind: SourceKind = "github_list"

    def openings(self, org: str, company: str, http: Http) -> list[Listing]:
        what = f"the {company} list"
        found: list[Listing] = []
        urls: set[str] = set()
        for row in answer_list(http.get_json(list_url(org), what=what, cap=LIST_CAP), what):
            # No category is left out here: the person's kinds and role words decide, and name their rule.
            if row.get("active") is False or row.get("is_visible") is False:
                continue
            url = text(row.get("url"))
            if not url or url in urls:
                continue
            urls.add(url)
            found.append(_listing(org, url, row))
        return found

    def detail(self, listing: Listing, http: Http) -> Detail:
        return from_listing(listing)


def _listing(org: str, url: str, row: Json) -> Listing:
    sponsorship = text(row.get("sponsorship"))
    return Listing(
        source_id=f"github_list:{org}",
        kind="github_list",
        org=org,
        external_id=text(row.get("id")) or url,
        company=text(row.get("company_name")),
        title=text(row.get("title")),
        location="; ".join(text(place) for place in items(row.get("locations"))),
        url=url,
        posted_at=timestamp(row.get("date_posted")),
        # The list says which terms a row is for; the title often doesn't.
        terms=tuple(text(term) for term in items(row.get("terms")) if text(term)),
        description=f"Sponsorship: {sponsorship}" if sponsorship in BLOCKS else "",
        alias=native_id(url),
    )

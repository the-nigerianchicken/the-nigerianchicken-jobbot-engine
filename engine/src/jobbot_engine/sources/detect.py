"""Which board a link belongs to, and which posting on it.

A person adds a source by pasting a link, and a GitHub list row points at a posting on some board. Both need
the same answer: is this a board jobbot can read, and under what name.
"""

from __future__ import annotations

import html as htmlmod
import re
from urllib.parse import parse_qs, urlparse

from jobbot_engine.model import SourceKind, posting_id
from jobbot_engine.sources.http import Http
from jobbot_engine.sources.workday import parts

# Boards people often link to that jobbot can't read. Their postings still arrive through the GitHub lists.
UNSUPPORTED_HOSTS = (
    "icims.com",
    "smartrecruiters.com",
    "oraclecloud.com",
    "taleo.net",
    "successfactors.com",
    "jobvite.com",
    "bamboohr.com",
    "workable.com",
)

_LOCALE = re.compile(r"[a-z]{2}(-[a-z]{2})?", re.I)
_OWN_HOST = re.compile(r"([a-z0-9-]+)\.(wd\d+\.myworkdayjobs\.com)")
_SHARED_HOST = re.compile(r"wd\d+\.myworkdaysite\.com")
_LINK = re.compile(r"""https?://[^\s"'<>)\\]+""")

# The company sites: a host (or a host and the start of a path) is the whole answer.
_SITES: tuple[tuple[str, str, SourceKind], ...] = (
    ("amazon.jobs", "", "amazon"),
    ("jobs.apple.com", "", "apple"),
    ("careers.google.com", "", "google"),
    ("google.com", "/about/careers", "google"),
    ("careers.microsoft.com", "", "microsoft"),
    ("shopify.com", "/careers", "shopify"),
    ("jobs.uber.com", "", "uber"),
    ("explore.jobs.netflix.net", "", "netflix"),
    ("jobs.netflix.com", "", "netflix"),
)


def _on(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _parts(path: str) -> list[str]:
    return [part for part in path.split("/") if part]


def from_url(url: str) -> tuple[SourceKind, str] | None:
    """(kind, org) for a link to a board jobbot reads, or None."""
    parsed = urlparse((url or "").strip())
    host, parts = parsed.netloc.lower(), _parts(parsed.path)
    if not host:
        return None
    if _on(host, "greenhouse.io"):
        org = _greenhouse(host, parts, parsed.query)
        return ("greenhouse", org) if org else None
    if _on(host, "lever.co"):
        org = _after(parts, ["v0", "postings"]) if host.startswith("api.") else (parts[0] if parts else None)
        return ("lever", org.lower()) if org else None
    if _on(host, "ashbyhq.com"):
        org = (
            _after(parts, ["posting-api", "job-board"])
            if host.startswith("api.")
            else (parts[0] if parts else None)
        )
        return ("ashby", org.lower()) if org else None
    if _on(host, "myworkdayjobs.com") or _on(host, "myworkdaysite.com"):
        org = _workday(host, parts)
        return ("workday", org) if org else None
    if host == "raw.githubusercontent.com":
        return _github_list(parts)
    if _on(host, "jobbank.gc.ca") or _on(host, "guichetemplois.gc.ca"):
        return ("jobbank", "canada")  # one board for all of Canada, searched for what its people want
    if _on(host, "linkedin.com") and parts[:1] == ["jobs"]:
        return ("linkedin", "jobs")  # one board for every job on LinkedIn, searched for its people's roles
    if _on(host, "uber.com") and "careers" in parts:
        return ("uber", "uber")
    for domain, path, kind in _SITES:
        if _on(host, domain) and parsed.path.startswith(path):
            return (kind, kind)
    return None


def _after(parts: list[str], prefix: list[str]) -> str | None:
    n = len(prefix)
    return parts[n] if parts[:n] == prefix and len(parts) > n else None


def _greenhouse(host: str, parts: list[str], query: str) -> str | None:
    if host.startswith("boards-api."):
        org = _after(parts, ["v1", "boards"])
    elif parts and parts[0] == "embed":
        org = (parse_qs(query).get("for") or [None])[0]
    else:
        org = parts[0] if parts else None
    return org.lower() if org else None


def _workday(host: str, parts: list[str]) -> str | None:
    """tenant|host|site, in the two shapes sources.workday.parts reads."""
    if parts and _LOCALE.fullmatch(parts[0]):
        parts = parts[1:]
    own = _OWN_HOST.fullmatch(host)
    if own:
        tenant, board_host = own.groups()
        if parts[:2] == ["wday", "cxs"] and len(parts) >= 4:
            return f"{parts[2]}|{board_host}|{parts[3]}"
        # A job link without its site starts with "job": no site can be read from it.
        return f"{tenant}|{board_host}|{parts[0]}" if parts and parts[0] != "job" else None
    if _SHARED_HOST.fullmatch(host) and len(parts) >= 3 and parts[0] != "wday":
        prefix, tenant, site = parts[:3]
        return f"{tenant}|{host}/{prefix}|{site}"
    return None


def _github_list(parts: list[str]) -> tuple[SourceKind, str] | None:
    # Imported here, since github_list imports native_id from this module.
    from jobbot_engine.sources.github_list import LISTS

    if len(parts) < 3 or parts[-1] != "listings.json":
        return None
    repository = f"{parts[0]}/{parts[1]}"
    for name, url in LISTS.items():
        if urlparse(url).path.startswith(f"/{repository}/"):
            return ("github_list", name)
    return ("github_list", repository)


def native_id(url: str) -> str | None:
    """The posting id jobbot gives this job when it reads the job's board itself, if the link says it."""
    parsed = urlparse((url or "").strip())
    host, parts = parsed.netloc.lower(), _parts(parsed.path)
    if _on(host, "greenhouse.io"):
        query = parse_qs(parsed.query)
        if "jobs" in parts:
            i = parts.index("jobs")
            if i >= 1 and i + 1 < len(parts) and parts[i + 1].isdigit():
                return posting_id("greenhouse", parts[i - 1].lower(), parts[i + 1])
        if query.get("for") and query.get("token"):
            return posting_id("greenhouse", query["for"][0].lower(), query["token"][0])
        return None
    if host == "jobs.lever.co" and len(parts) >= 2:
        return posting_id("lever", parts[0].lower(), parts[1])
    if host == "jobs.ashbyhq.com" and len(parts) >= 2:
        return posting_id("ashby", parts[0].lower(), parts[1])
    if _on(host, "amazon.jobs") and "jobs" in parts:
        i = parts.index("jobs")
        if i + 1 < len(parts):
            return posting_id("amazon", "amazon", parts[i + 1])
    return None


def from_page(url: str, http: Http) -> tuple[SourceKind, str] | None:
    """The board behind a link: the link's own board, or else the first board linked from its page."""
    found = from_url(url)
    if found:
        return found
    page = htmlmod.unescape(http.get_text(url, what=urlparse(url).netloc or url))
    for link in _LINK.findall(page):
        found = from_url(link)
        if found:
            return found
    return None


def board_url(kind: str, org: str, fallback: str) -> str:
    """The address of a board, as a person would open it."""
    if kind == "greenhouse":
        return f"https://boards.greenhouse.io/{org}"
    if kind == "lever":
        return f"https://jobs.lever.co/{org}"
    if kind == "ashby":
        return f"https://jobs.ashbyhq.com/{org}"
    if kind == "workday":
        return parts(org)[0]
    if kind == "jobbank":
        return "https://www.jobbank.gc.ca/jobsearch/jobsearch"
    if kind == "linkedin":
        return "https://www.linkedin.com/jobs/search"
    return fallback

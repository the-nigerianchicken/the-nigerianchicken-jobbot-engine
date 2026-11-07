"""A GitHub-list posting's description, read from the page its link leads to.

The lists carry no descriptions, so a rule about what a posting says can't judge their rows, and he can't read
them in the app. Most job pages carry the posting as JSON-LD, which is read first; Fitzii, amazon.jobs,
TikTok, ByteDance and SuccessFactors career sites write it in the page's own boxes instead. The job sites
whose pages are built in the browser, or whose list links lead to a page without it (Greenhouse,
SmartRecruiters, Oracle, Workable, Lever's apply page), are asked through their own APIs.
A short link (go.vansh.nyc) is followed to the page it stands for, and the site is known from there.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote, urlparse

from jobbot_engine import places
from jobbot_engine.sources.html import strip
from jobbot_engine.sources.http import Http, NotThere, Refused, SourceError, Waits

log = logging.getLogger(__name__)

# A JSON-LD description shorter than this is a teaser, and a job site's API, if there's one, has the posting.
FULL = 300
# Pages read at once: enough to describe a run's new postings in seconds, few enough to be polite.
WORKERS = 8
MOST = 20000  # the contract's longest description
_LD = re.compile(r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I)
_FITZII_BOX = re.compile(r"<div id=[\"']description[\"'][^>]*>", re.I)
_FITZII_LABEL = re.compile(r"<span class=[\"']posting-attribute[\"']>.*?</span>", re.S | re.I)
# amazon.jobs: each part of the posting is a section that opens with its heading.
_AMAZON_SECTION = re.compile(r"<div class=[\"']section[\"']>(?=\s*<h2)", re.I)
# TikTok and ByteDance: a short heading paragraph, then the part's text, its line breaks kept.
_HEADED = re.compile(
    r"<p\b[^>]*>([^<]{1,60})</p>\s*<p class=[\"'][^\"']*\bwhitespace-pre-line\b[^\"']*[\"']>(.*?)</p>",
    re.S | re.I,
)
# A SuccessFactors career site, on whatever domain the company gives it.
_SUCCESSFACTORS_BOX = re.compile(r"<span\b[^>]*\bclass=[\"']jobdescription[\"'][^>]*>", re.I)

Ask = Callable[[Http], str]


@dataclass(frozen=True)
class Page:
    """What a list posting's page says: its description, and the one country the job's own site puts it in
    when that site says it's at one place only (Workday). None when the site doesn't say, says more places,
    or wasn't asked."""

    text: str
    country: str | None = None


Read = Page | SourceError | None  # what describe_all says of each link


def describe(http: Http, url: str) -> str:
    """The description as readable text, or "" when nothing jobbot can read has one.

    The page itself not answering raises its SourceError, so the caller can say why, and an ask that waits
    (the run's time, a resting host) raises too. A job site's API failing otherwise leaves what the page had.
    """
    final, page = http.get_page(url)
    found = from_page(page) or _from_box(final, page)
    if len(found) >= FULL:
        return found
    ask = _api(final)
    if ask is None:
        return found
    try:
        return ask(http) or found
    except Waits:
        raise
    except (SourceError, LookupError, TypeError, AttributeError):  # gone, or not the shape expected
        return found


def sole_country(http: Http, url: str) -> str | None:
    """The country a Workday job's own record puts it in, when it names no other place; None for a link
    that isn't a Workday job, or a record that names more places or a country jobbot doesn't know.

    The record's place names themselves aren't taken: they're often Workday's own codes and addresses
    ("CA-AB-Calgary", "Customer Support Center"), read worse than the list's.
    """
    api = _workday_api(url)
    if api is None:
        return None
    info = cast(dict[str, Any], http.get_json(api, what="the Workday job's record")["jobPostingInfo"])
    if info.get("additionalLocations"):
        return None
    country = cast(dict[str, Any], info.get("country") or {})
    return places.home_country(str(country.get("descriptor") or ""))


def _workday_api(url: str) -> str | None:
    """A Workday job link's record in Workday's API (sources/workday.py), or None for any other link."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    parts = [part for part in parsed.path.split("/") if part]
    if "job" not in parts:
        return None
    at = parts.index("job")
    # The language a link may start with (en-US) isn't part of the board's address.
    before = [part for part in parts[:at] if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", part)]
    if host.endswith(".myworkdayjobs.com"):
        tenant, sites = host.split(".")[0], before
    elif host.endswith(".myworkdaysite.com") and len(before) == 3 and before[0] == "recruiting":
        tenant, sites = before[1], before[2:]
    else:
        return None
    if len(sites) != 1 or at + 1 >= len(parts):
        return None
    job = "/".join(parts[at + 1 :])
    return f"https://{host}/wday/cxs/{tenant.replace('-', '_')}/{sites[0]}/job/{job}"


def describe_all(
    http: Http, urls: Sequence[str], stop_by: float | None, listed: Sequence[str] = ()
) -> list[Read]:
    """Each link's description, 8 read at once, until time.monotonic() `stop_by`. One link failing never
    stops the others. `listed` is the place the list gives each link: where it names a country alone, the
    job's own record is asked for its country too (sole_country), and that ask waiting or failing in a way
    that passes is the page's, so the country is asked again with it later.

    A Page is what was read: its text "" when the page is gone, refuses jobbot, or has nothing it can read,
    which is final. A SourceError is a page that was asked and failed in a way that passes (down, too slow,
    turning the run away): it's tried again later, a limited number of times. None is a page not asked in
    time.
    """
    asks = [places.names_a_country(place) for place in listed] or [False] * len(urls)

    def one(url: str, ask: bool) -> Read:
        if stop_by is not None and time.monotonic() >= stop_by:
            return None
        try:
            text = describe(http, url)
        except Waits:  # raised before the page was asked
            return None
        except (NotThere, Refused) as error:
            log.info("no description at %s: %s", url, error)
            return Page("")
        except SourceError as error:
            return error
        except Exception:  # a page jobbot misread: it stays without one
            log.exception("couldn't read the description at %s", url)
            return Page("")
        if not ask:
            return Page(text)
        try:
            return Page(text, sole_country(http, url))
        except Waits:
            return None
        except (NotThere, Refused) as error:  # final: the list's place stands
            log.info("no Workday record for %s: %s", url, error)
            return Page(text)
        except SourceError as error:
            return error
        except Exception:  # a record jobbot misread: the list's place stands
            log.exception("couldn't read the Workday record for %s", url)
            return Page(text)

    if not urls:
        return []
    # Not a with block: its end waits for every page, and a page still answering at `stop_by` is left to
    # finish on its own while the run goes on.
    pool = ThreadPoolExecutor(max_workers=min(WORKERS, len(urls)))
    futures = [pool.submit(one, url, ask) for url, ask in zip(urls, asks, strict=True)]
    wait(futures, timeout=None if stop_by is None else max(0.0, stop_by - time.monotonic()))
    pool.shutdown(wait=False, cancel_futures=True)
    return [future.result() if future.done() and not future.cancelled() else None for future in futures]


def with_page(listed: str, text: str) -> str:
    """The description the list gave (a sponsorship line, or nothing) and then the page's, cut to fit. One
    that already holds the page's text, read before and read again, stays as it is."""
    if text and text[:FULL] in listed:
        return listed
    joined = "\n\n".join(part for part in (listed, text) if part)
    return joined if len(joined) <= MOST else joined[: MOST - 1].rstrip() + "…"


def from_page(page: str) -> str:
    """The first JobPosting description in the page's JSON-LD, as text; "" when there's none."""
    for block in _LD.findall(page):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for posting in _postings(data):
            text = strip(str(posting.get("description") or ""))
            if text:
                return text
    return ""


def _from_box(url: str, page: str) -> str:
    """The description a job site without JSON-LD writes into its page, as text; "" when there's none."""
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if host == "fitzii.com":
        return _fitzii(page)
    if host == "amazon.jobs":
        return "\n\n".join(_box_text(_inside(page, at.end(), "div")) for at in _AMAZON_SECTION.finditer(page))
    if host in ("lifeattiktok.com", "joinbytedance.com", "jobs.bytedance.com"):
        # The text is plain, written with its own line breaks, so it isn't flattened like a box's HTML.
        parts = (f"{strip(heading)}\n{strip(text)}" for heading, text in _HEADED.findall(page))
        return "\n\n".join(part for part in parts if part.strip())
    box = _SUCCESSFACTORS_BOX.search(page)
    return _box_text(_inside(page, box.end(), "span")) if box else ""


def _fitzii(page: str) -> str:
    """Fitzii writes the description in its `description` box, after a "Description:" label."""
    start = _FITZII_BOX.search(page)
    if start is None:
        return ""
    inside = _FITZII_LABEL.sub("", _inside(page, start.end(), "div"), count=1)
    # The page drops its own line breaks once it loads: kept, they'd put a blank line between every two.
    return _box_text(re.sub(r"(?i)<br\s*/?>", "", inside))


def _inside(page: str, at: int, tag: str) -> str:
    """What's inside the `tag` element whose opening tag ends at `at`: to its own closing tag, past the
    ones nested in it, or to the page's end."""
    depth = 1
    for found in re.finditer(rf"<(/?){tag}\b[^>]*>", page[at:], re.I):
        depth += -1 if found.group(1) else 1
        if depth == 0:
            return page[at : at + found.start()]
    return page[at:]


def _box_text(html: str) -> str:
    """A box's HTML as text. A line ending in its HTML is only a space: kept, it would split a sentence."""
    return strip(re.sub(r"\s+", " ", html.replace("&nbsp;", " ")))


def _postings(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, list):
        for item in cast(list[Any], node):
            yield from _postings(item)
    elif isinstance(node, dict):
        found = cast(dict[str, Any], node)
        kind = found.get("@type")
        if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
            yield found
        for key in ("@graph", "mainEntity"):
            if key in found:
                yield from _postings(found[key])


def _joined(*parts: Any) -> str:
    return "\n".join(text for text in (strip(str(part or "")) for part in parts) if text)


def _api(url: str) -> Ask | None:
    """How to ask the job site behind `url` for its posting, or None when it isn't one jobbot knows."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    parts = [part for part in parsed.path.split("/") if part]
    if host.endswith("greenhouse.io") and "jobs" in parts[1:-1]:
        at = parts.index("jobs")
        board, job = quote(parts[at - 1]), quote(parts[at + 1])
        api = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job}"
        return lambda http: _joined(http.get_json(api, what=f"Greenhouse posting {job}")["content"])
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and len(parts) >= 2:
        org, job = quote(parts[0]), quote(parts[1])
        api = f"https://api.{host.removeprefix('jobs.')}/v0/postings/{org}/{job}"

        def lever(http: Http) -> str:
            posting = http.get_json(api, what=f"Lever posting {job}")
            lists = cast(list[dict[str, Any]], posting.get("lists") or [])
            headed = (part for item in lists for part in (item.get("text"), item.get("content")))
            return _joined(posting.get("description"), *headed, posting.get("additional"))

        return lever
    if host == "jobs.smartrecruiters.com" and len(parts) >= 2:
        company, job = quote(parts[0]), quote(parts[1].split("-")[0])
        api = f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{job}"

        def smartrecruiters(http: Http) -> str:
            sections = http.get_json(api, what=f"SmartRecruiters posting {job}")["jobAd"]["sections"]
            return _joined(*(section.get("text") for section in sections.values()))

        return smartrecruiters
    if host.endswith(".oraclecloud.com") and "sites" in parts[:-1] and "job" in parts[:-1]:
        site, job = quote(parts[parts.index("sites") + 1]), quote(parts[parts.index("job") + 1])
        api = (
            f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
            f"?expand=all&onlyData=true&finder=ById;Id=%22{job}%22,siteNumber={site}"
        )

        def oracle(http: Http) -> str:
            items = http.get_json(api, what=f"Oracle posting {job}")["items"]
            if not items:
                return ""
            item = items[0]
            keys = ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")
            return _joined(*(item.get(key) for key in keys))

        return oracle
    if host == "apply.workable.com" and len(parts) >= 3 and parts[1] == "j":
        account, job = quote(parts[0]), quote(parts[2])
        api = f"https://apply.workable.com/api/v2/accounts/{account}/jobs/{job}"

        def workable(http: Http) -> str:
            posting = http.get_json(api, what=f"Workable posting {job}")
            return _joined(posting.get("description"), posting.get("requirements"), posting.get("benefits"))

        return workable
    return None

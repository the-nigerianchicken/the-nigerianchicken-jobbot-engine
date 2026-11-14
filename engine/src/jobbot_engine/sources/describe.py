"""A posting's description, read from the page its link leads to.

The GitHub lists carry no descriptions, and neither do Job Bank's feed, LinkedIn's search or Microsoft's
(PAGE_KINDS), so a rule about what a posting says can't judge them, and he can't read them in the app. Most
job pages carry the posting as JSON-LD, which is read first; Fitzii, amazon.jobs, TikTok, ByteDance,
SuccessFactors career sites, Job Bank, Greenhouse's embedded pages, Rippling and Apple write it in the page's
own boxes or data instead. The job sites whose pages are built in the browser, or whose list links lead to a
page without it (Greenhouse, SmartRecruiters, Oracle, Workable, Lever's apply page), are asked through their
own APIs, and LinkedIn, Microsoft and Tesla only through theirs, their pages having nothing for jobbot.
LinkedIn doesn't welcome automated reading, so its pages are asked one at a time, LINKEDIN_GAP apart.
A short link (go.vansh.nyc) is followed to the page it stands for, and the site is known from there.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote, urlparse

from jobbot_engine import places
from jobbot_engine.model import SourceKind
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
# Job Bank keeps the whole posting, as plain text, in a hidden span; the page shows only parts of it.
_JOBBANK_BOX = re.compile(r"<span\b[^>]*\bproperty=[\"']description[\"'][^>]*>(.*?)</span>", re.S | re.I)
# Greenhouse's newer pages, its embedded ones among them: the description box, before the form.
_GREENHOUSE_BOX = re.compile(r"<div\b[^>]*\bclass=[\"']job__description\b[^\"']*[\"'][^>]*>", re.I)
# A Next.js page's own data, which Rippling's job pages are built from.
_NEXT_DATA = re.compile(r"<script\b[^>]*\bid=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", re.S | re.I)
# Apple's job page builds itself from this, a JSON string inside the page's script.
_APPLE_DATA = re.compile(r'__staticRouterHydrationData\s*=\s*JSON\.parse\(("(?:[^"\\]|\\.)*")\)', re.S)
# LinkedIn's public job page, the one its job search shows: the description is its "show more" box.
_LINKEDIN_BOX = re.compile(r"<div\b[^>]*\bclass=[\"']show-more-less-html__markup\b[^\"']*[\"'][^>]*>", re.I)
_LINKEDIN_JOB = re.compile(r"/jobs/view/(?:[^/?#]*-)?(\d{6,})")

# The kinds of source whose listings come without a description, so each posting's page is read for one, as
# soon as it's found and later for the ones stored before (scheduler/read.py and run.py).
PAGE_KINDS: frozenset[SourceKind] = frozenset({"github_list", "jobbank", "linkedin", "microsoft"})
# Seconds between two LinkedIn pages: its own search asks one every 6 (sources/linkedin.py).
LINKEDIN_GAP = 3.0

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
    direct = _direct(url)
    if direct is not None:
        return direct(http)
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

    def lightly(items: list[tuple[int, str, bool]]) -> None:
        """LinkedIn's pages, one at a time and LINKEDIN_GAP apart, in a worker of their own; each read is
        kept as it comes, so the time running out keeps the ones already read."""
        for done, (at, url, ask) in enumerate(items):
            if done:
                _pause(LINKEDIN_GAP)
            if stop_by is not None and time.monotonic() >= stop_by:
                return
            lane_reads[at] = one(url, ask)

    if not urls:
        return []
    pairs = list(zip(urls, asks, strict=True))
    slow = [(at, url, ask) for at, (url, ask) in enumerate(pairs) if _is_linkedin(url)]
    lane_reads: dict[int, Read] = {}
    # Not a with block: its end waits for every page, and a page still answering at `stop_by` is left to
    # finish on its own while the run goes on.
    pool = ThreadPoolExecutor(max_workers=min(WORKERS, len(urls)) + (1 if slow else 0))
    futures = {at: pool.submit(one, url, ask) for at, (url, ask) in enumerate(pairs) if not _is_linkedin(url)}
    lane = pool.submit(lightly, slow) if slow else None
    waiting: list[Future[Any]] = [*futures.values(), *([lane] if lane is not None else [])]
    wait(waiting, timeout=None if stop_by is None else max(0.0, stop_by - time.monotonic()))
    pool.shutdown(wait=False, cancel_futures=True)
    reads: list[Read] = [None] * len(urls)
    for at, future in futures.items():
        reads[at] = future.result() if future.done() and not future.cancelled() else None
    for at, read in dict(lane_reads).items():
        reads[at] = read
    return reads


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _is_linkedin(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com")


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
    if host == "jobbank.gc.ca":
        return _jobbank(page)
    if host.endswith("greenhouse.io"):
        box = _GREENHOUSE_BOX.search(page)
        return _box_text(_inside(page, box.end(), "div")) if box else ""
    if host == "ats.rippling.com":
        return _rippling(page)
    if host == "jobs.apple.com":
        return _apple(page)
    box = _SUCCESSFACTORS_BOX.search(page)
    return _box_text(_inside(page, box.end(), "span")) if box else ""


def _jobbank(page: str) -> str:
    """Job Bank's hidden description is plain text with its own line breaks, its entities escaped again for
    each site the posting passed through (&amp;amp;nbsp;)."""
    box = _JOBBANK_BOX.search(page)
    if box is None:
        return ""
    text = box[1]
    for _ in range(4):
        unescaped = html.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    lines = (" ".join(line.replace("\xa0", " ").split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _rippling(page: str) -> str:
    data = _NEXT_DATA.search(page)
    if data is None:
        return ""
    try:
        post = json.loads(data[1])["props"]["pageProps"]["apiData"]["jobPost"]
    except (ValueError, LookupError, TypeError):
        return ""
    about = cast(dict[str, Any], post).get("description") if isinstance(post, dict) else None
    if isinstance(about, dict):
        return _joined(*cast(dict[str, Any], about).values())
    return _joined(about)


def _apple(page: str) -> str:
    """Apple's page data: the summary and description, then the qualifications under their own headings."""
    data = _APPLE_DATA.search(page)
    if data is None:
        return ""
    try:
        job = _apple_job(json.loads(json.loads(data[1])))
    except (ValueError, TypeError):
        return ""
    if job is None:
        return ""
    headed = (
        ("Minimum qualifications", job.get("minimumQualifications")),
        ("Preferred qualifications", job.get("preferredQualifications")),
        ("Responsibilities", job.get("responsibilities")),
    )
    parts = [job.get("jobSummary"), job.get("description")]
    parts += [f"{heading}\n{text}" for heading, text in headed if isinstance(text, str) and text.strip()]
    return _joined(*parts)


def _apple_job(node: Any) -> dict[str, Any] | None:
    """The first `jobsData` in Apple's page data."""
    if isinstance(node, dict):
        found = cast(dict[str, Any], node)
        if isinstance(found.get("jobsData"), dict):
            return cast(dict[str, Any], found["jobsData"])
        node = list(found.values())
    if isinstance(node, list):
        for item in cast(list[Any], node):
            job = _apple_job(item)
            if job is not None:
                return job
    return None


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


def _direct(url: str) -> Ask | None:
    """How to ask a job site whose page has nothing jobbot can read (built in the browser, or behind a
    sign-in), in place of the page; None for every other site."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    parts = [part for part in parsed.path.split("/") if part]
    job = _LINKEDIN_JOB.search(parsed.path) if _is_linkedin(url) else None
    if job is not None:
        number = job[1]
        guest = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{number}"

        def linkedin(http: Http) -> str:
            _, page = http.get_page(guest, what=f"LinkedIn job {number}")
            box = _LINKEDIN_BOX.search(page)
            return _box_text(_inside(page, box.end(), "div")) if box else ""

        return linkedin
    if host == "apply.careers.microsoft.com" and len(parts) >= 3 and parts[-2] == "job":
        position = quote(parts[-1])
        api = (
            "https://apply.careers.microsoft.com/api/pcsx/position_details"
            f"?position_id={position}&domain=microsoft.com&hl=en"
        )
        return lambda http: _joined(
            http.get_json(api, what=f"Microsoft job {position}")["data"].get("jobDescription")
        )
    tesla_job = re.search(r"(\d+)$", parts[-1]) if parts else None
    if host in ("tesla.com", "www.tesla.com") and "job" in parts[:-1] and tesla_job is not None:
        api = f"https://www.tesla.com/cua-api/careers/job/{tesla_job[1]}"

        def tesla(http: Http) -> str:
            posting = http.get_json(api, what=f"Tesla job {tesla_job[1]}")
            keys = ("jobDescription", "jobResponsibilities", "jobRequirements", "jobCompensationAndBenefits")
            return _joined(*(posting.get(key) for key in keys))

        return tesla
    return None


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

"""Finding a followed company's board when nobody has linked to it.

A company someone follows can be missing from every list: on 2026-09-24, 39 of 81 followed companies were.
This asks Greenhouse, Lever and Ashby directly whether they host a board under the company's name.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable
from typing import Any

from jobbot_engine.model import SourceKind
from jobbot_engine.sources.base import items, obj
from jobbot_engine.sources.http import HostResting, Http, OutOfTime, SourceError, TurnedAway, Waits


def slugs(name: str) -> list[str]:
    """The shapes a company's name usually takes in a board's address."""
    plain = re.sub(r"[^a-z0-9]+", "", name.lower())
    spaced = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return list(dict.fromkeys([plain, spaced, spaced.replace("-", "")]))


def _greenhouse(slug: str, http: Http) -> int:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    return len(items(obj(http.get_json(url, what=f"Greenhouse board {slug}")).get("jobs")))


def _lever(slug: str, http: Http) -> int:
    data: Any = http.get_json(
        f"https://api.lever.co/v0/postings/{slug}", {"mode": "json"}, what=f"Lever board {slug}"
    )
    return len(items(data))


def _ashby(slug: str, http: Http) -> int:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    return len(items(obj(http.get_json(url, what=f"Ashby board {slug}")).get("jobs")))


BOARDS: tuple[tuple[SourceKind, Callable[[str, Http], int]], ...] = (
    ("greenhouse", _greenhouse),
    ("lever", _lever),
    ("ashby", _ashby),
)


def find(company: str, http: Http, budget: int = 9) -> tuple[SourceKind, str, int] | None:
    """(kind, org, how many jobs) for the first board with jobs, asking at most `budget` times."""
    asked = 0
    unanswered: Waits | None = None
    for slug, (kind, count) in itertools.product(slugs(company), BOARDS):
        if asked >= budget:
            break
        asked += 1
        try:
            jobs = count(slug, http)
        except OutOfTime:
            raise
        except Waits as error:
            unanswered = error
            continue
        except TurnedAway:
            unanswered = HostResting(f"a board host turned the lookup for {company} away")
            continue
        except SourceError:
            continue
        if jobs:
            return (kind, slug, jobs)
    if unanswered is not None:  # not "no board": one of the places it could be didn't answer
        raise unanswered
    return None

"""Company names: how they're compared, and whether a followed company is this posting's company."""

from __future__ import annotations

import re
from functools import cache


@cache  # every posting is compared with every followed name, and the same names come back each time
def norm(s: str | None) -> str:
    """Lowercase words separated by single spaces, with all punctuation gone."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def key(s: str | None) -> str:
    """The name with nothing but its letters and digits, for comparing two spellings of one company."""
    return norm(s).replace(" ", "")


def follows(follow: str, company: str, org: str) -> bool:
    """Whether a followed company is this posting's company.

    Whole words, never substrings: in the old engine "Ada" fired inside a name like "Nevada Partners".
    A one-word follow matches any word of the company or board name ("Google" in "Google DeepMind"). A
    longer one must be the whole name: "Jane Street" matches "janestreet", not "Street Group".
    """
    wanted = norm(follow)
    if not wanted:
        return False
    for name in (company, org):
        n = norm(name)
        if n and (wanted in n.split() or n.replace(" ", "") == wanted.replace(" ", "")):
            return True
    return False


@cache
def _wanted(follow_list: tuple[str, ...]) -> tuple[frozenset[str], frozenset[str]]:
    """The follows as `follows` compares them: each as words, and with nothing but letters and digits."""
    wanted = [norm(f) for f in follow_list if norm(f)]
    return frozenset(wanted), frozenset(w.replace(" ", "") for w in wanted)


def follows_any(follow_list: tuple[str, ...], company: str, org: str) -> bool:
    """Whether any of these follows is this posting's company, as `follows` says, without comparing each."""
    words, whole = _wanted(follow_list)
    for name in (company, org):
        n = norm(name)
        if n and (not words.isdisjoint(n.split()) or n.replace(" ", "") in whole):
            return True
    return False

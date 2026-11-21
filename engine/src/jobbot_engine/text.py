"""Whole-word matching, the only kind of matching the rules use.

Plain substring matching let "intern" match "internal" and "international" in the old engine, which pulled
full-time roles into the results. A word here matches only between word boundaries. A phrase allows a space,
hyphen or slash between its words. A word the person ends with `*` is a prefix ("data scien*" matches
"Data Scientist"), and it still has to start at a word boundary.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

# An accented Latin letter is part of a word ("pr" isn't in "Préposé"); other scripts aren't letters here.
_LETTER = "a-z0-9\u00e0-\u00f6\u00f8-\u024f"  # the text is lowercase by then
_BEFORE = rf"(?<![{_LETTER}])"
_AFTER = rf"(?![{_LETTER}])"


@lru_cache(maxsize=8192)
def pattern(word: str) -> re.Pattern[str]:
    """The compiled pattern for one cleaned, lowercase word."""
    prefix = word.endswith("*")
    body = word[:-1].strip() if prefix else word
    compiled = _BEFORE + re.escape(body).replace(r"\ ", r"[\s\-/]+")
    return re.compile(compiled if prefix else compiled + _AFTER)


def clean(word: str) -> str:
    return " ".join(word.lower().split())


def matches(words: Iterable[str], text: str | None) -> list[str]:
    """The words that occur in `text`, in the order given, ignoring case."""
    haystack = (text or "").lower()
    found: list[str] = []
    for word in words:
        cleaned = clean(word)
        if cleaned in ("", "*"):
            continue
        if pattern(cleaned).search(haystack):
            found.append(word)
    return found


def first_hit(words: Iterable[str], text: str | None) -> str | None:
    """The text as written where the first of `words` occurs in it ("Intern" for "intern"), or None."""
    original = text or ""
    haystack = original.lower()
    for word in words:
        cleaned = clean(word)
        if cleaned in ("", "*"):
            continue
        hit = pattern(cleaned).search(haystack)
        if hit:
            return original[hit.start() : hit.end()]
    return None

"""Reading careers pages that aren't APIs: the postings a page ships inside it, and its words without tags."""

from __future__ import annotations

import json
import re
from typing import Any, cast

from jobbot_engine.sources.base import Json

# A page built with a JavaScript framework ships its data as a string inside a script tag; Apple's whole board
# arrives as JSON.parse("..."). Decoding that string is the only way to read it: unescaping it by hand loses
# every posting whose description contains a quote, which was 17 of Apple's 20.
_LONG_STRING = re.compile(r'"((?:[^"\\]|\\.){400,}?)"', re.S)
# How far to walk out from a field to the braces around its object, either way.
_REACH = 60000


def payload(html: str) -> str:
    """The page, plus every long JavaScript string in it, decoded."""
    parts = [html]
    for match in _LONG_STRING.finditer(html):
        try:
            parts.append(json.loads('"' + match.group(1) + '"'))
        except ValueError:
            continue
    return "\n".join(parts)


def objects_with(text: str, key: str, limit: int = 400) -> list[Json]:
    """Every JSON object in `text` that carries `key`.

    The blob is nested and far too large to parse whole, so this walks out from each mention of the field to
    the braces around it.
    """
    found: list[Json] = []
    for hit in re.finditer('"' + re.escape(key) + '"\\s*:', text):
        start = _opening_brace(text, hit.start())
        if start is None:
            continue
        end = _closing_brace(text, start)
        if end is None:
            continue
        try:
            value: Any = json.loads(text[start : end + 1])
        except ValueError:
            continue
        if isinstance(value, dict):
            found.append(cast(Json, value))
        if len(found) >= limit:
            break
    return found


def _opening_brace(text: str, at: int) -> int | None:
    depth = 0
    for i in range(at, max(-1, at - _REACH), -1):
        if text[i] == "}":
            depth += 1
        elif text[i] == "{":
            if depth == 0:
                return i
            depth -= 1
    return None


def _closing_brace(text: str, start: int) -> int | None:
    depth = 0
    for j in range(start, min(len(text), start + _REACH)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    return None


def words(html: str) -> str:
    """The text of a fragment of HTML on one line, without its tags."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()

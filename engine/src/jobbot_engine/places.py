"""Which countries a posting's place names, from schema/data/places.json.

A place jobbot can't read names no country, so the Places rule shows it: an unknown place is never a reason to
hide a job. The app reads the same file (app/src/shared/rules.ts) for the countries and regions it offers.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from functools import cache
from typing import Any

from jobbot_engine.schema import data_dir
from jobbot_engine.text import first_hit


@cache
def catalogue() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((data_dir() / "places.json").read_text(encoding="utf-8"))
    return loaded


def _code(piece: str) -> tuple[str, str, str] | None:
    """(country, code, the city it follows) when a piece of the place is a state or province code, written in
    capitals: "TX" after a comma, "Olathe KS" or "MN-Mankato". A lowercase "or" is a word, not Oregon."""
    for country, codes in catalogue()["codes"].items():
        for code in (c.upper() for c in codes):
            if re.fullmatch(rf"{code}(\s+-.*|\s*\(.*)?", piece):
                return country, code, ""
            city = re.fullmatch(rf"(.+?)\s+{code}|{code}-(.+)", piece)
            if city:
                return country, code, city.group(1) or city.group(2)
    return None


def _country_of(piece: str) -> str | None:
    """The country a piece of the place names, by one of its places or codes."""
    code = _code(piece)
    if code is not None and not code[2]:
        return code[0]
    return next((c for c, words in catalogue()["places"].items() if first_hit(words, piece)), None)


def _countries(segment: str) -> list[str]:
    """The countries one place in a list names. A country written last, or its two-letter code written
    first, says where all of it is ("San Jose, Costa Rica", "IT, Turin"), and a state code says which country
    the city before it is in ("Vienna, VA"); a code after another country's province is that country's own
    ("Milton, Ontario, CA")."""
    entry = catalogue()
    pieces = [p.strip() for p in segment.split(",") if p.strip()]
    if not pieces:
        return []
    first = pieces[0]
    for country, iso in entry["iso"].items():
        short = first.isupper() and first.lower() in entry["names"].get(country, [])
        if len(pieces) > 1 and (first == iso or short):
            return [country]
    last = pieces[-1].strip(" .()").lower()
    for country in entry["places"]:
        if len(pieces) > 1 and last in entry["names"].get(country, [country.lower()]):
            return [country]
    found: list[tuple[int, str]] = []
    claimed: set[int] = set()
    for i, piece in enumerate(pieces):
        code = _code(piece)
        if code is None or (not code[2] and i == 0):
            continue
        country, written, city = code
        if not city:
            before = _country_of(pieces[i - 1])
            # "Toronto, CA" is Canada and "Berlin, DE" Germany: a country's own code after one of its cities.
            if before is not None and entry["iso"].get(before) == written:
                country = before
            if i >= 2 and before is not None and before != country:
                continue
            claimed.add(i - 1)
        claimed.add(i)
        found.append((i, country))
    for i, piece in enumerate(pieces):
        if i not in claimed:
            found += [(i, c) for c, words in entry["places"].items() if first_hit(words, piece)]
    return [country for _, country in sorted(found)]


def countries_in(place: str) -> list[str]:
    """Every country the place names, in the catalogue's order. Places are split on ";", "|" or "or"."""
    seen: set[str] = set()
    for segment in re.split(r"[;|]| or ", place):
        seen.update(_countries(segment))
    return [c for c in catalogue()["places"] if c in seen]


def _named_alone(piece: str) -> str | None:
    """The country a place in a list is when it's nothing but that country's name ("Canada", "USA"). A code
    alone ("CA") isn't one: it's California as often as Canada."""
    entry = catalogue()
    plain = piece.strip(" .").casefold()
    for country in entry["places"]:
        if plain in (country.casefold(), *entry["names"].get(country, [])):
            return str(country)
    return None


def country_named(piece: str) -> str | None:
    """The country a piece is when it's nothing but its name ("Canada", "United States of America")."""
    return _named_alone(piece)


def names_a_country(location: str) -> bool:
    """Whether one of the places a list gives ("Canada; Santa Clara, CA") is a country named alone."""
    return any(_named_alone(piece) for piece in location.split(";"))


def keep_countries(location: str, countries: Sequence[str]) -> str:
    """The place without the countries named alone that aren't among `countries`, the ones the job's own
    site says it's in: the Simplify list gave NVIDIA's Santa Clara jobs "Canada", which NVIDIA never said.
    A city or province stays whatever its country, and a place left with nothing is the first of
    `countries`."""
    pieces = [piece.strip() for piece in location.split(";") if piece.strip()]
    kept = [piece for piece in pieces if _named_alone(piece) in (None, *countries)]
    if kept == pieces:
        return location
    return "; ".join(kept) if kept else countries[0]


def is_remote(place: str) -> bool:
    return first_hit(["remote"], place) is not None


def expand(chosen: Iterable[str]) -> set[str]:
    """The countries these choices name: a region is its countries, a country itself."""
    regions: dict[str, list[str]] = catalogue()["regions"]
    found: set[str] = set()
    for name in chosen:
        found.update(regions.get(name, [name]))
    return found


def home_country(written: str) -> str | None:
    """The catalogue's country a home country names, however it's written: "USA", "us", "Ireland"."""
    entry = catalogue()
    plain = written.strip().casefold()
    for country in entry["places"]:
        names = [country.casefold(), *entry["names"].get(country, [])]
        if plain in names or plain == str(entry["iso"].get(country, "")).casefold():
            return str(country)
    return None

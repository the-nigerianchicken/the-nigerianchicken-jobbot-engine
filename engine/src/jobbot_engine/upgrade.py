"""Settings written before plain rules, read as rules.

Two older forms are read: `filters`, which showed and hid values of twelve attributes, and the first
`rules`, a list of typed rules. Each becomes the plain rules that keep out what it kept out, as nearly as
the rules can, and never more: a choice the plain rules can't say (a graduation date, the eligibility check,
a word group that only flagged) is dropped, so it shows jobs rather than hiding them. The app does the same
when it reads a stored document (app/src/shared/upgrade.ts); both are checked against
schema/fixtures/upgrade.json, so the engine and the app never read one person's settings two ways. The old
import (legacy.py) goes through here too.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

from jobbot_engine import places
from jobbot_engine.rules.judge import catalogue

KINDS = {"internship": "Internship", "co_op": "Co-op", "new_grad": "New grad"}
SEASONS = ("Winter", "Spring", "Summer", "Fall")
# The fewest days old a job in each hidden age bucket was when found; Found late hides from a week at least.
AGE_FROM_DAYS = {"Same day": 0, "1–3 days": 1, "4–14 days": 4, "Older": 15}
LEAST_DAYS = 7
_SEASON_YEAR = re.compile(r"(winter|spring|summer|fall|autumn)\s+(20\d\d)")
_YEAR = re.compile(r"20\d\d")


def blank_rules() -> dict[str, Any]:
    """Every rule off, with nothing chosen, but Repeats, which hides only copies of a job already shown."""
    return {
        "kind": {"on": False, "chosen": [], "words": []},
        "companies": {"on": False, "words": []},
        "ruled_out": {"on": False, "chosen": [], "words": []},
        "roles": {"on": False, "chosen": [], "words": []},
        "places": {"on": False, "chosen": [], "remote": True},
        "terms": {"on": False, "chosen": []},
        "sponsorship": {"on": False, "words": []},
        "age": {"on": False, "days": LEAST_DAYS},
        "repeats": {"on": True},
    }


def _plain(word: str) -> str:
    return word.lower().rstrip("*").strip()


def _contains(text: str, word: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None


def _own(words: Iterable[str], given: Iterable[str]) -> list[str]:
    """The words the catalogue doesn't already give: the person's own."""
    known = {_plain(w) for w in given}
    return list(dict.fromkeys(w for w in words if _plain(w) not in known))


def _preset_words(presets: Mapping[str, Any], name: str) -> list[str]:
    preset: Any = presets.get(name, [])
    return list(cast(list[str], preset["words"] if isinstance(preset, dict) else preset))


def _add(rule: dict[str, Any], words: Iterable[str]) -> None:
    rule["words"] = list(dict.fromkeys([*rule["words"], *words]))


def _terms(words: Iterable[str]) -> list[str]:
    """The terms these words name: "summer 2027" is Summer 2027; "2027" alone is every term of 2027."""
    found: list[str] = []
    for word in words:
        plain = _plain(word)
        season = _SEASON_YEAR.fullmatch(plain)
        if season:
            name = "Fall" if season[1] == "autumn" else season[1].title()
            found.append(f"{name} {season[2]}")
        elif _YEAR.fullmatch(plain):
            found += [f"{s} {plain}" for s in SEASONS]
    return list(dict.fromkeys(found))


def _country(word: str) -> str | None:
    """The catalogue's country this word names: by its name, one of its places, or one of its codes."""
    plain, entry = _plain(word), places.catalogue()
    for country, found in entry["places"].items():
        names = [country.lower(), *found, *entry["names"].get(country, []), *entry["codes"].get(country, [])]
        if plain in names:
            return str(country)
    return None


def _ruled_out(words: Sequence[str], rule: dict[str, Any]) -> None:
    """Title words to rule out: one a choice gives ("senior") chooses it, so the catalogue's exceptions hold
    ("Member of Technical Staff" isn't Staff); the rest are the person's own."""
    presets: Mapping[str, Any] = catalogue()["ruled_out"]
    for word in words:
        name = next((n for n in presets if _plain(word) in _preset_words(presets, n)), None)
        if name is None:
            _add(rule, [word])
        elif name not in rule["chosen"]:
            rule["chosen"].append(name)
    rule["chosen"].sort(key=list(presets).index)


def _finish(rules: dict[str, Any]) -> dict[str, Any]:
    """Companies and Words you rule out are on whenever they have something to hide."""
    for name in ("companies", "ruled_out"):
        rule = rules[name]
        rule["on"] = bool(rule.get("chosen") or rule["words"])
    return rules


def rules_from_filters(filters: Mapping[str, Any]) -> dict[str, Any]:
    """Rules that keep out what these filters kept out."""
    attributes: Mapping[str, Any] = filters["attributes"]
    entry = catalogue()
    rules = blank_rules()

    def hides(attribute: str, value: str) -> bool:
        a = attributes[attribute]
        return value in a["hidden"] or (value not in a["shown"] and a["new"] == "hide")

    def words_of(attribute: str, value: str) -> list[str]:
        return list(attributes[attribute].get("words", {}).get(value, []))

    # Job type and Field kept a job out only when Other was hidden: then what's still shown is chosen.
    for name, attribute, presets in (("kind", "job_type", "kinds"), ("roles", "field", "roles")):
        if hides(attribute, "Other"):
            # A level of job (Entry level) is newer than these filters: they never chose it.
            chosen = [v for v, w in entry[presets].items() if isinstance(w, list) and not hides(attribute, v)]
            given = [w for v in chosen for w in entry[presets][v]]
            own = _own([w for v in chosen for w in words_of(attribute, v)], given)
            rules[name] = {"on": True, "chosen": chosen, "words": own}

    ruled_out = rules["ruled_out"]
    ruled_out["chosen"] = [v for v in entry["ruled_out"] if v in attributes["level"]["hidden"]]
    for value in ruled_out["chosen"]:
        _add(ruled_out, _own(words_of("level", value), _preset_words(entry["ruled_out"], value)))
    yours = attributes["yours"]
    for group in yours.get("groups", []):
        if group["name"] not in yours["hidden"]:
            continue  # a group only tagged its jobs: it hid nothing
        if group["field"] == "title":
            _ruled_out(group["words"], ruled_out)
        elif group["field"] == "company":
            _add(rules["companies"], group["words"])

    countries = list(places.catalogue()["places"])
    if any(hides("location", c) for c in countries):
        chosen = [c for c in countries if not hides("location", c)]
        rules["places"] = {"on": True, "chosen": chosen, "remote": not hides("location", "Remote")}

    # The rule shows only the terms chosen, so a filter that showed terms it hadn't seen can't become it
    # without hiding more: it converts to the rule off.
    term = attributes["term"]
    if term["new"] == "hide":
        chosen = [v for v in term["shown"] if _terms([v]) == [v]]
        rules["terms"] = {"on": bool(chosen), "chosen": chosen}

    hidden_ages = [days for value, days in AGE_FROM_DAYS.items() if value in attributes["age"]["hidden"]]
    if hidden_ages:
        rules["age"] = {"on": True, "days": max(LEAST_DAYS, min(hidden_ages))}

    if hides("sponsorship", "Won't sponsor"):
        own = _own(words_of("sponsorship", "Won't sponsor"), entry["sponsorship"])
        rules["sponsorship"] = {"on": True, "words": own}
    return _finish(rules)


def rules_from_legacy(legacy: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Rules that keep out what the first rules, a typed list, removed. A rule that's off hides nothing."""
    entry = catalogue()
    rules = blank_rules()
    for rule in legacy:
        if not rule["enabled"]:
            continue
        kind, action, params = rule["type"], rule["action"], rule["params"]
        if kind == "role_kinds":
            chosen = [KINDS[k] for k in params["kinds"] if k in KINDS]
            given = [w for v in chosen for w in entry["kinds"][v]]
            words = [w for k in params["kinds"] for w in params["words"].get(k, [])]
            rules["kind"] = {"on": True, "chosen": chosen, "words": _own(words, given)}
        elif kind == "role_words":
            named: set[str] = set()
            own: list[str] = []
            presets: Mapping[str, list[str]] = entry["roles"]
            for word in params["words"]:
                found = {v for v, ws in presets.items() if any(_contains(_plain(word), w) for w in ws)}
                named |= found
                if not found:
                    own.append(word)
            chosen = [v for v in entry["roles"] if v in named]
            rules["roles"] = {"on": True, "chosen": chosen, "words": list(dict.fromkeys(own))}
        elif kind in ("ruled_out_words", "custom", "hidden_companies") and action == "remove":
            field = params.get("field", "company" if kind == "hidden_companies" else "title")
            words = params.get("words", params.get("companies", []))
            if field == "title":
                _ruled_out(words, rules["ruled_out"])
            elif field == "company":
                _add(rules["companies"], words)
        elif kind == "terms":
            chosen = _terms(params["wanted"])
            rules["terms"] = {"on": bool(chosen), "chosen": chosen}
        elif kind == "places":
            allowed = [*params["allowed_countries"], *params["allowed_regions"], *params["preferred"]]
            named_countries = {_country(w) for w in allowed}
            chosen = [c for c in places.catalogue()["places"] if c in named_countries]
            remote = any(_plain(w) == "remote" for w in allowed) or params["keep_unknown"]
            rules["places"] = {"on": bool(chosen), "chosen": chosen, "remote": remote}
        elif kind == "sponsorship" and action == "remove":
            rules["sponsorship"] = {"on": True, "words": _own(params["phrases"], entry["sponsorship"])}
        elif kind == "age":
            days = max(LEAST_DAYS, math.ceil(params["max_hours"] / 24))
            rules["age"] = {"on": True, "days": min(days, 365)}
    return _finish(rules)


def upgrade_settings(doc: Mapping[str, Any]) -> dict[str, Any]:
    """The document in the new terms: `filters`, or the first `rules`, become plain rules. One already
    upgraded comes back as it is, with Repeats on if it was saved before there was that rule."""
    upgraded = {k: v for k, v in doc.items() if k not in ("filters", "rules")}
    rules = doc.get("rules")
    if isinstance(rules, Mapping):
        saved = cast("Mapping[str, Any]", rules)
        upgraded["rules"] = {**saved, "repeats": saved.get("repeats", {"on": True})}
    elif "filters" in doc:
        upgraded["rules"] = rules_from_filters(doc["filters"])
    else:
        upgraded["rules"] = rules_from_legacy(rules or [])
    return upgraded

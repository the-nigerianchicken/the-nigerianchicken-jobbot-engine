"""The old engine's settings and saved postings, read into the new engine's terms.

`old_rules` turns the old `criteria.yaml` (plus the overrides he saved in the old app) into rules that
reproduce the old engine's answers, in its order; `convert` makes those a settings document, read as plain
rules the way any document written with the first rules is (upgrade.py). Part 5's import reuses this.

The old file names the person's home country in two of its keys. The converter finds those keys by their
shape, so the country's name never has to appear in this repo.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from jobbot_engine.model import Posting, SourceKind, content_hash, parse_stamp, posting_id, stamp
from jobbot_engine.upgrade import rules_from_legacy

# The old app's level switches, and the words each one put in titles (old jobbot/settings.py).
LEVELS = {
    "internship": [
        "intern", "interns", "internship", "internships", "co-op", "coop", "co op", "student", "work term",
    ],
    "new_grad": [
        "new grad", "new graduate", "early career", "entry level", "graduate program", "emerging talent",
        "university grad",
    ],
}  # fmt: skip
CO_OP = ["co-op", "coop", "co op", "work term"]
SEASONS = ["Fall 2026", "Winter 2027", "Spring 2027", "Summer 2027", "Fall 2027",
           "Winter 2028", "Spring 2028", "Summer 2028"]  # fmt: skip

# Old drop codes, and the rule that keeps the same postings out now. "skipped" is Kind of job. The old
# eligibility check (not_eligible) has none: those postings are shown now.
OLD_TO_RULE = {
    "title_excluded": "r_ruled_out",
    "not_wanted_role": "r_roles",
    "seniority": "r_ruled_out",
    "no_level_signal": "skipped",
    "wrong_term": "r_terms",
    "no_term": "r_terms",
    "location_excluded": "r_places",
    "outside_geography": "r_places",
    "place_off": "r_places",
    "needs_us_authorization": "r_sponsorship",
    "too_old": "r_age",
}

_TOKEN = re.compile(r"[a-z0-9]+")


def words_in(texts: Iterable[str]) -> set[str]:
    """Every whole word in these texts, lowercased."""
    found: set[str] = set()
    for text in texts:
        found.update(_TOKEN.findall((text or "").lower()))
    return found


# Endings that make another form of the same word: "engineer" -> "engineering", "developer" -> "developers".
INFLECTIONS = ("s", "es", "ing", "ings", "ed", "er", "ers")


def _starred(words: Iterable[str], known: set[str]) -> list[str]:
    """The old engine matched these lists as substrings. Whole-word matching keeps that, with a `*`, where it
    was doing real work:

    - a word whose last part never stands alone in real postings is a fragment ("data scien", "manufactur");
    - a word that real postings also use with an ending ("software engineer" in "Software Engineering").

    A longer, different word ("intern" in "Internal", "sales" in "Salesforce") is what whole-word matching is
    for, so it doesn't earn a `*`.
    """
    out: list[str] = []
    for word in words:
        tokens = _TOKEN.findall(word.lower())
        last = tokens[-1] if tokens else ""
        prefix = last and (last not in known or any(last + end in known for end in INFLECTIONS))
        out.append(f"{word}*" if prefix and not word.endswith("*") else word)
    return list(dict.fromkeys(out))


def _key(section: Mapping[str, Any], pattern: str) -> str:
    """The one key in `section` matching `pattern`: the old file's keys that name the home country."""
    keys = [k for k in section if re.fullmatch(pattern, k)]
    if len(keys) != 1:
        raise ValueError(f"expected one key like {pattern!r}, found {keys}")
    return keys[0]


def _rule(
    rule_id: str, rule_type: str, name: str, action: str, params: dict[str, Any], enabled: bool = True
) -> dict[str, Any]:
    return {
        "id": rule_id,
        "type": rule_type,
        "name": name,
        "enabled": enabled,
        "action": action,
        "params": params,
    }


def _effective(criteria: Mapping[str, Any], search: Mapping[str, Any] | None) -> dict[str, Any]:
    """The old engine's apply_search: his saved overrides laid over the file."""
    terms, titles = criteria["terms"], criteria["titles"]
    fresh: Mapping[str, Any] = criteria.get("freshness") or {}
    tiers: Mapping[Any, Any] = fresh.get("max_age_hours_by_tier") or {}
    out: dict[str, Any] = {
        "unwanted": list(terms["exclude_any"]),
        "wanted": list(terms["require_any"]),
        "level_words": list(terms["require_in_title_any"]),
        "new_grad": False,
        "roles_wanted": list(titles["require_any"]),
        "roles_excluded": [w for w in titles["exclude_any"] if w.lower() not in LEVELS["new_grad"]],
        "max_hours": int(fresh.get("max_age_hours", 24)),
        "max_hours_followed": int(tiers.get(1, tiers.get("1", fresh.get("max_age_hours", 24)))),
        "places_excluded": [],
        "sponsorship": True,
    }
    if search is None:
        return out
    seasons = {s.lower() for s in SEASONS}
    switches = search["seasons"]
    out["unwanted"] = [w for w in out["unwanted"] if w.lower() not in seasons] + [
        s.lower() for s, on in switches.items() if not on
    ]
    out["wanted"] = list(dict.fromkeys(out["wanted"] + [s.lower() for s, on in switches.items() if on]))
    levels: Mapping[str, Any] = search.get("levels") or {}
    out["level_words"] = [w for lvl, on in levels.items() if on for w in LEVELS.get(lvl, [])] or out[
        "level_words"
    ]
    out["new_grad"] = bool(levels.get("new_grad"))
    out["roles_wanted"] = list(search["roles_wanted"])
    out["roles_excluded"] = [w for w in search["roles_excluded"] if w.lower() not in LEVELS["new_grad"]]
    out["max_hours"] = int(search["window_hours"])
    out["max_hours_followed"] = int(search["window_following_hours"])
    switches_places: Mapping[str, Any] = search.get("places") or {}
    off = [place for place, on in switches_places.items() if not on]
    if off:
        raise ValueError(f"place switches turned off can't be converted to rules: {', '.join(off)}")
    out["places_excluded"] = list(search.get("places_excluded") or [])
    out["sponsorship"] = bool(search.get("hide_no_sponsor", True))
    return out


def old_rules(
    criteria: Mapping[str, Any], search: Mapping[str, Any] | None, *, corpus_words: set[str] | None = None
) -> list[dict[str, Any]]:
    """Rules that give the old engine's answers, in its order and in the terms of the first settings."""
    known = set(corpus_words or ())
    eff = _effective(criteria, search)
    titles, places, eligibility = criteria["titles"], criteria["locations"], criteria["eligibility"]
    home = places[_key(places, r"[a-z]+_markers")]
    no_sponsor = eligibility[_key(eligibility, r"drop_outside_[a-z]+_if_description_contains")]
    window = eligibility["graduation_window"]
    kinds = ["internship", "co_op", *(["new_grad"] if eff["new_grad"] else [])]

    rules = [
        _rule("r_role_kinds", "role_kinds", "Kinds of role", "keep_only", {
            "kinds": kinds,
            "words": {"internship": eff["level_words"], "co_op": CO_OP, "new_grad": LEVELS["new_grad"]},
        }),
        _rule("r_ruled_out", "ruled_out_words", "Words that rule a posting out", "remove",
              {"words": _starred(eff["roles_excluded"], known)}),
        _rule("r_role_words", "role_words", "Role words", "keep_only",
              {"words": _starred(eff["roles_wanted"], known)}),
        _rule("r_seniority", "ruled_out_words", "Seniority", "remove",
              {"words": list(titles["seniority_exclude"])}),
        _rule("r_terms", "terms", "Terms", "keep_only", {
            "wanted": _starred(eff["wanted"], known),
            "unwanted": _starred(eff["unwanted"], known),
            "keep_when_no_term": True,
        }),
        _rule("r_places", "places", "Places", "keep_only", {
            "allowed_countries": list(places["allowed_countries"]),
            "allowed_regions": list(places.get("allowed_regions") or []),
            "home_markers": list(home),
            "excluded": list(places["exclude_any"]) + eff["places_excluded"],
            "preferred": list(places["preferred"]),
            "keep_unknown": True,
        }),
        _rule("r_sponsorship", "sponsorship", "Sponsorship", "remove",
              {"phrases": list(no_sponsor) or ["no sponsorship"]},
              enabled=eff["sponsorship"] and bool(no_sponsor)),
        _rule("r_eligibility", "eligibility_check", "Eligibility check by Claude", "remove",
              {"judge": ["citizenship", "clearance", "degree"]}),
        _rule("r_age", "age", "Age", "remove",
              {"max_hours": eff["max_hours"], "max_hours_followed": eff["max_hours_followed"]}),
        _rule("r_borderline", "custom", "Borderline roles", "flag",
              {"field": "title", "words": _starred(titles.get("borderline_any") or ["-"], known)},
              enabled=bool(titles.get("borderline_any"))),
        _rule("r_description_flags", "custom", "Description flags", "flag",
              {"field": "description",
               "words": list(eligibility.get("flag_if_description_contains") or ["-"])},
              enabled=bool(eligibility.get("flag_if_description_contains"))),
        _rule("r_graduation", "graduation", "Graduation date", "flag",
              {"earliest": str(window[0])[:7], "latest": str(window[-1])[:7]}),
        _rule("r_hidden", "hidden_companies", "Companies you hide", "remove", {"companies": []}),
    ]  # fmt: skip
    return rules


def convert(
    criteria: Mapping[str, Any],
    search: Mapping[str, Any] | None,
    details: Mapping[str, Any],
    now: datetime,
    *,
    corpus_words: set[str] | None = None,
    hidden_companies: Sequence[str] = (),
) -> dict[str, Any]:
    """A complete settings document, version 1, whose rules give the old engine's answers as nearly as plain
    rules can. `hidden_companies` are the ones he muted in the old app."""
    rules = old_rules(criteria, search, corpus_words=corpus_words)
    for rule in rules:
        if rule["type"] == "hidden_companies":
            rule["params"]["companies"] = list(hidden_companies)
    return {
        "version": 1,
        "updated_at": stamp(now),
        "details": dict(details),
        "rules": rules_from_legacy(rules),
        "alert_rules": [],
        "display": {"order": "newest", "grouping": "none", "theme": "system"},
        "misses": "add",
        "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00", "time_zone": "UTC"},
    }


def follows_from_targets(text: str) -> list[str]:
    """The old targets.txt: the companies he follows, one word each, separated by whitespace."""
    return text.split()


def posting_from_corpus(row: Mapping[str, Any], first_seen_at: datetime) -> Posting:
    """A saved posting from the old engine's corpus, with its old uid as its id."""
    kind: SourceKind = "github_list" if row["source"] == "community" else row["source"]
    external = str(row.get("raw_id") or row["url"])
    fields: dict[str, Any] = {
        "id": posting_id(kind, row["org"], external),
        "source_id": f"{kind}:{row['org']}",
        "external_id": external,
        "company": row["company"] or row["org"],
        "title": row["title"],
        "location": row.get("location") or "",
        "url": row["url"],
        "apply_url": row.get("apply_url") or row["url"],
        "posted_at": parse_stamp(row.get("posted_at")),
        "deadline": parse_stamp(row.get("deadline")),
        "remote": bool(row.get("remote")),
        "terms": tuple(row.get("terms") or ()),
        "description": row.get("description") or "",
        "first_seen_at": first_seen_at,
        "closed_at": None,
        "content_hash": "",
    }
    fields["content_hash"] = content_hash(fields)
    return Posting(**fields)

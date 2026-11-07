"""The judge: one posting, one person's rules, one explained outcome.

The rules are a short fixed list (schema/data/rules.json), applied in its order:

1. Kind of job: a title with no word of a chosen kind, that fits no chosen level (Entry level: none of the
   words above it, and one of the person's roles), isn't collected (skipped, never sent).
2. Otherwise the first rule that hides the posting names it (filtered, r_<rule>, with what it matched), and
   every other rule that would have is a note.
3. Found late never hides a job at a company the person follows; a note says it would have.

A rule that's off, or has nothing chosen, hides nothing; neither does a place jobbot doesn't recognise or a
job that names no term. A rule that fails shows the posting with a note: an error never hides a job (rule 7).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from functools import cache
from typing import Any, cast

from jobbot_engine import places
from jobbot_engine.model import Posting
from jobbot_engine.rules.alerts import alert_reason
from jobbot_engine.rules.base import Context, Outcome, is_followed
from jobbot_engine.schema import data_dir
from jobbot_engine.terms import term_of
from jobbot_engine.text import clean, first_hit, pattern


@cache
def catalogue() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((data_dir() / "rules.json").read_text(encoding="utf-8"))
    return loaded


RULE_IDS: tuple[str, ...] = tuple(rule["id"] for rule in catalogue()["rules"])


def name_of(rule: str) -> str:
    return str(next(r["name"] for r in catalogue()["rules"] if r["id"] == rule))


def rule_id_of(rule: str) -> str:
    return f"r_{rule}"


def _words(rule: Mapping[str, Any], presets: Mapping[str, Any]) -> list[str]:
    """A rule's words: its chosen presets' words, then the person's own."""
    found: list[str] = []
    for name in rule.get("chosen", []):
        preset: Any = presets.get(name, [])
        found += cast(list[str], preset["words"] if isinstance(preset, dict) else preset)
    return [*found, *rule.get("words", [])]


def _said(names: list[str]) -> str:
    return " or ".join(names) if len(names) < 3 else ", ".join(names[:-1]) + " or " + names[-1]


# Each check answers what hid the posting, or None. It's given every rule, and only asked about one that's on.
Found = tuple[str, ...] | None
Check = Callable[[Posting, Mapping[str, Any], Context], Found]


def _levels(kinds: Mapping[str, Any], chosen: list[str]) -> list[tuple[str, list[str]]]:
    """The chosen kinds that are a level of job ("Entry level"): their names and the words above them."""
    return [(n, kinds[n]["above"]) for n in chosen if isinstance(kinds.get(n), dict) and "above" in kinds[n]]


def _kind(posting: Posting, rules: Mapping[str, Any], context: Context) -> Found:
    """A title is collected when it has a word of a chosen kind, or fits a chosen level: no word of a more
    senior job and, while Roles is on, one of its words."""
    rule, roles, title = rules["kind"], rules["roles"], posting.title
    kinds: Mapping[str, Any] = catalogue()["kinds"]
    words, levels = _words(rule, kinds), _levels(kinds, rule["chosen"])
    if not words and not levels:
        return None
    if first_hit(words, title) is not None:
        return None
    level_names = {n for n, _ in levels}
    named = [*(n for n in rule["chosen"] if n not in level_names), *(["your"] if rule["words"] else [])]
    why = [f"no {_said(named)} word in the title"] if named else []
    role_words = _words(roles, catalogue()["roles"]) if roles["on"] else []
    for name, above in levels:
        hit = _unwanted(above, title, _spans(catalogue()["ignore"], title))
        if hit is not None:
            why.append(f"{hit} is above {name.lower()}")
        elif role_words and first_hit(role_words, title) is None:
            role_names = [*roles["chosen"], *(["your"] if roles["words"] else [])]
            why.append(f"no {_said(role_names)} word in the title")
        else:
            return None
    return (", and ".join(why),)


def _companies(posting: Posting, rules: Mapping[str, Any], context: Context) -> Found:
    hit = first_hit(rules["companies"]["words"], posting.company)
    return None if hit is None else (hit,)


def _spans(words: list[str], text: str) -> list[tuple[int, int]]:
    low = text.lower()
    return [
        (m.start(), m.end())
        for w in words
        if (c := clean(w)) not in ("", "*")
        for m in pattern(c).finditer(low)
    ]


def _unwanted(words: list[str], title: str, wanted: list[tuple[int, int]]) -> str | None:
    """The first of `words` in the title, as written, not inside a strictly longer wanted span."""
    for start, end in _spans(words, title):
        if not any(s <= start and end <= e and e - s > end - start for s, e in wanted):
            return title[start:end]
    return None


def _ruled_out(posting: Posting, rules: Mapping[str, Any], context: Context) -> Found:
    """A ruled-out word counts unless it sits inside a strictly longer phrase the person wants (a role word)
    or the catalogue names ("staff" in "Member of Technical Staff"). The same phrase on both lists still
    hides: it was ruled out. A preset with `unless` words doesn't hide a title that has one of them."""
    rule, roles, title = rules["ruled_out"], rules["roles"], posting.title
    wanted = _spans(catalogue()["ignore"], title)
    if roles["on"]:
        wanted += _spans(_words(roles, catalogue()["roles"]), title)
    presets: Mapping[str, Any] = catalogue()["ruled_out"]
    groups: list[tuple[list[str], list[str]]] = [
        (presets[n]["words"], presets[n].get("unless", [])) for n in rule["chosen"] if n in presets
    ]
    for words, unless in [*groups, (rule["words"], [])]:
        if unless and first_hit(unless, title) is not None:
            continue
        hit = _unwanted(words, title, wanted)
        if hit is not None:
            return (hit,)
    return None


def _roles(posting: Posting, rules: Mapping[str, Any], context: Context) -> Found:
    rule = rules["roles"]
    words = _words(rule, catalogue()["roles"])
    if not words or first_hit(words, posting.title) is not None:
        return None
    named = [*rule["chosen"], *(["your"] if rule["words"] else [])]
    return (f"no {_said(named)} word in the title",)


def _places(posting: Posting, rules: Mapping[str, Any], context: Context) -> Found:
    rule = rules["places"]
    wanted = places.expand(rule["chosen"])
    found = places.countries_in(posting.location)
    if not wanted or not found or set(found) & wanted:
        return None
    if rule["remote"] and (posting.remote or places.is_remote(posting.location)):
        return None
    return tuple(found)


def _terms(posting: Posting, rules: Mapping[str, Any], context: Context) -> Found:
    rule = rules["terms"]
    named = term_of(" ".join(posting.terms)) or term_of(posting.title, posting.description)
    if not rule["chosen"] or not named or set(named) & set(rule["chosen"]):
        return None
    return tuple(named)


def _sponsorship(posting: Posting, rules: Mapping[str, Any], context: Context) -> Found:
    if not context.details.get("needs_sponsorship_outside_home"):
        return None
    home = places.home_country(str(context.details.get("home_country") or ""))
    if home is not None and home in places.countries_in(posting.location):
        return None
    hit = first_hit([*catalogue()["sponsorship"], *rules["sponsorship"]["words"]], posting.description)
    return None if hit is None else (hit,)


def _age(posting: Posting, rules: Mapping[str, Any], context: Context) -> Found:
    if posting.posted_at is None:
        return None
    days = max(0, (posting.first_seen_at - posting.posted_at).days)
    return (f"{days} days old when found",) if days >= rules["age"]["days"] else None


def _repeats(posting: Posting, rules: Mapping[str, Any], context: Context) -> Found:
    return None if context.shown_instead is None else (context.shown_instead,)


CHECKS: dict[str, Check] = {
    "kind": _kind,
    "companies": _companies,
    "ruled_out": _ruled_out,
    "roles": _roles,
    "places": _places,
    "terms": _terms,
    "sponsorship": _sponsorship,
    "age": _age,
    "repeats": _repeats,
}


def _judge(posting: Posting, settings: Mapping[str, Any], context: Context) -> Outcome:
    rules = settings["rules"]
    hidden: list[tuple[str, tuple[str, ...]]] = []
    notes: list[str] = []
    for rule_id in RULE_IDS:
        if not rules[rule_id]["on"]:
            continue
        try:
            found = CHECKS[rule_id](posting, rules, context)
        except Exception:  # one rule failing never stops the others, or hides the posting
            notes.append(f"{name_of(rule_id)} couldn't be checked")
            continue
        if found is None:
            continue
        if rule_id == "kind":
            return Outcome("skipped", rule_id_of(rule_id), name_of(rule_id), found, (), None)
        if rule_id == "age" and is_followed(posting, context.follows):
            notes.append(
                f"{name_of(rule_id)} would hide it ({', '.join(found)}), but you follow {posting.company}"
            )
            continue
        hidden.append((rule_id, found))
    if not hidden:
        return Outcome("new", None, None, (), tuple(notes), None)
    (rule_id, found), others = hidden[0], hidden[1:]
    notes += [f"Also hidden by {name_of(other)}" for other, _ in others]
    return Outcome("filtered", rule_id_of(rule_id), name_of(rule_id), found, tuple(notes), None)


def evaluate(posting: Posting, settings: Mapping[str, Any], context: Context) -> Outcome:
    """The posting's outcome under the person's rules, with why it alerts when it does."""
    outcome = _judge(posting, settings, context)
    reason = alert_reason(posting, outcome, settings, context.follows)
    return replace(outcome, alert_reason=reason) if reason else outcome


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_NO_CONTEXT = Context(now=_EPOCH, details={}, follows=())


def searches(settings: Mapping[str, Any]) -> list[str]:
    """What a person wants a board that only answers searches searched for, besides internships: when Kind of
    job takes a level of job (Entry level), their roles' search words and their own role words."""
    rules = settings["rules"]
    kind, roles = rules["kind"], rules["roles"]
    if not (kind["on"] and roles["on"] and _levels(catalogue()["kinds"], kind["chosen"])):
        return []
    named: Mapping[str, list[str]] = catalogue()["searches"]
    words = [w for name in roles["chosen"] for w in named.get(name, [])]
    words += [clean(w.replace("*", " ")) for w in roles["words"]]
    return list(dict.fromkeys(w for w in words if w))


def collects(title: str, settings: Mapping[str, Any]) -> bool:
    """Whether a posting with this title is collected at all: Kind of job doesn't skip it."""
    return skip_reason(title, settings) is None


def skip_reason(title: str, settings: Mapping[str, Any]) -> str | None:
    """Why Kind of job doesn't collect a posting with this title, or None when it does."""
    rule = settings["rules"]["kind"]
    if not rule["on"]:
        return None
    probe = Posting(
        id="0" * 16, source_id="", external_id="", company="", title=title, location="", url="", apply_url="",
        posted_at=None, deadline=None, remote=False, terms=(), description="", first_seen_at=_EPOCH,
        closed_at=None, content_hash="",
    )  # fmt: skip
    try:
        found = _kind(probe, settings["rules"], _NO_CONTEXT)
    except Exception:  # a failing rule never keeps a job from being collected
        return None
    return None if found is None else found[0]

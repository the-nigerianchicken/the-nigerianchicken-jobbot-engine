"""Judging postings: the person's rules, and the judge that explains every decision."""

from jobbot_engine.rules import judge
from jobbot_engine.rules.base import Context, Outcome, is_followed
from jobbot_engine.rules.judge import RULE_IDS, collects, evaluate, rule_id_of, searches, skip_reason

__all__ = [
    "RULE_IDS",
    "Context",
    "Outcome",
    "collects",
    "evaluate",
    "is_followed",
    "judge",
    "rule_id_of",
    "searches",
    "skip_reason",
]

"""Why a posting alerts: the person follows its company, or one of their alert rules matches it.

Only postings that stay in New alert. The app alerts only the first time a posting enters
New for that person, so judging the same posting again never alerts twice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from jobbot_engine.model import Posting
from jobbot_engine.rules.base import Outcome, field_text, is_followed
from jobbot_engine.text import matches


def alert_reason(
    posting: Posting, outcome: Outcome, settings: Mapping[str, Any], follows: Sequence[str]
) -> str | None:
    if outcome.state != "new":
        return None
    if is_followed(posting, follows):
        return f"You follow {posting.company}"
    for rule in settings["alert_rules"]:
        if rule["enabled"] and matches(rule["words"], field_text(posting, rule["field"])):
            return f"Alert rule: {rule['name']}"
    return None

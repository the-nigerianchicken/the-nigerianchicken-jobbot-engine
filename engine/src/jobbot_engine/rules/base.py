"""What judging a posting knows besides the posting, and what it gives back."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from jobbot_engine.model import Posting
from jobbot_engine.names import follows_any

State = Literal["new", "filtered", "skipped"]


@dataclass(frozen=True)
class Context:
    """What judging may know besides the posting: the time, the person's details and follows, and where
    the job is shown instead when this posting is a copy of one already shown (repeats.py)."""

    now: datetime
    details: Mapping[str, Any]
    follows: tuple[str, ...]
    shown_instead: str | None = None


@dataclass(frozen=True)
class Outcome:
    """A posting's judgement: hidden by `rule_id` (r_<rule>), `matched` what it found; or new."""

    state: State
    rule_id: str | None
    rule_name: str | None
    matched: tuple[str, ...]
    notes: tuple[str, ...]
    alert_reason: str | None


def org_of(posting: Posting) -> str:
    return posting.source_id.split(":", 1)[1] if ":" in posting.source_id else ""


def is_followed(posting: Posting, follow_list: Sequence[str]) -> bool:
    return follows_any(tuple(follow_list), posting.company, org_of(posting))


def field_text(posting: Posting, field_name: str) -> str:
    """The text of one of the fields a word group can read: title, description, company or place."""
    return {
        "title": posting.title,
        "description": posting.description,
        "company": posting.company,
        "place": posting.location,
    }[field_name]

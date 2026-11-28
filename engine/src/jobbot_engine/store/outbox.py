"""How the outbox's records are checked on the way in and grouped on the way out.

Postings, decisions and closings travel together in postings batches of at most 100 of each, so one request
stays inside the Worker's free computing limit. Records are grouped strictly in the order they were queued,
so a decision always arrives in the batch of its posting or a later one, and a posting never shares
a batch with its own closing, where the order between them would be lost. A run summary, a coverage report, an
added source travels alone, in its place in that order.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Literal

from jobbot_engine.schema import validate

OutboxKind = Literal["posting", "decision", "closed", "run", "coverage", "sources"]
BatchKind = Literal["postings", "run", "coverage", "sources"]
MOST = 100

# Where each kind of record goes in a postings batch, and the schema a lone record is checked against.
_IN_BATCH: dict[str, str] = {"posting": "postings", "decision": "decisions", "closed": "closed"}
_ALONE: dict[str, tuple[BatchKind, str]] = {
    "run": ("run", "run-summary"),
    "coverage": ("coverage", "coverage-report"),
    "sources": ("sources", "sources-add"),
}


@dataclass(frozen=True)
class OutboxBatch:
    kind: BatchKind
    seqs: tuple[int, ...]
    body: dict[str, Any]
    # Every record in it was checked under the contract in force now (Store.outbox_batches).
    checked: bool = False


def check_record(kind: str, record: dict[str, Any]) -> None:
    """Raise ValueError, naming the schema, if `record` isn't one the app will accept."""
    if kind in _IN_BATCH:
        one = _empty()
        one[_IN_BATCH[kind]].append(record)
        name, errors = "postings-batch", validate("postings-batch", one)
    elif kind in _ALONE:
        name = _ALONE[kind][1]
        errors = validate(name, record)
    else:
        raise ValueError(f"the outbox has no kind {kind!r}")
    if errors:
        raise ValueError(f"a {kind} record doesn't match {name}: {'; '.join(errors[:5])}")


def _empty() -> dict[str, list[Any]]:
    return {"postings": [], "decisions": [], "closed": []}


def _clashes(body: dict[str, list[Any]], kind: str, record: dict[str, Any]) -> bool:
    """Whether a posting and its closing would share the batch, where their order would be lost."""
    if kind == "posting":
        return any(c["posting_id"] == record["id"] for c in body["closed"])
    if kind == "closed":
        return any(p["id"] == record["posting_id"] for p in body["postings"])
    return False


def batches(rows: Iterable[tuple[int, str, dict[str, Any]]]) -> Iterator[OutboxBatch]:
    """The queued records as batches to send, in order."""
    body, seqs = _empty(), list[int]()
    for seq, kind, record in rows:
        if kind in _IN_BATCH:
            field = _IN_BATCH[kind]
            if len(body[field]) == MOST or _clashes(body, kind, record):
                yield OutboxBatch("postings", tuple(seqs), dict(body))
                body, seqs = _empty(), []
            body[field].append(record)
            seqs.append(seq)
            continue
        if seqs:
            yield OutboxBatch("postings", tuple(seqs), dict(body))
            body, seqs = _empty(), []
        yield OutboxBatch(_ALONE[kind][0], (seq,), record)
    if seqs:
        yield OutboxBatch("postings", tuple(seqs), dict(body))

"""The engine's local state, in SQLite: what it knows, how each source is doing, and what's waiting to send.

Every method that writes is one transaction; a read is one statement, which needs no transaction of its
own and so never waits for a writer. The run reads sources on several threads, so one connection is shared
under a lock, and the database waits up to 5 seconds for another process's write instead of failing. With
the write-ahead log, synchronous NORMAL keeps the database whole through a crash or a power cut while
syncing the disk only at checkpoints, not on every commit.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Collection, Generator, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jobbot_engine.model import Posting, stamp
from jobbot_engine.repeats import Copy, links
from jobbot_engine.schema import contract_digest
from jobbot_engine.store.migrations import migrate
from jobbot_engine.store.outbox import OutboxBatch, OutboxKind, batches, check_record
from jobbot_engine.store.rows import (
    DecisionRow,
    KnownRow,
    ListRow,
    SkippedRow,
    SourceState,
    must_time,
    time_or_none,
)


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._write_ahead_log()
        self._db.execute("PRAGMA synchronous = NORMAL")
        with self._lock:
            migrate(self._db)

    def _write_ahead_log(self) -> None:
        """Turn the write-ahead log on. That takes the file for a moment, and SQLite doesn't wait for it, so
        two processes opening a new database at once (the run and the jobs service) try again for 5 s."""
        for _ in range(50):
            try:
                self._db.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as error:
                if "locked" not in str(error):
                    raise
                time.sleep(0.1)
        self._db.execute("PRAGMA journal_mode = WAL")

    def close(self) -> None:
        self._db.close()

    def pragma(self, name: str) -> Any:
        return self._db.execute(f"PRAGMA {name}").fetchone()[0]

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._db.execute(sql, args).fetchone()
            return row

    def _all(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # What's known ---------------------------------------------------------------------------------------

    def known(self, posting_id: str) -> KnownRow | None:
        row = self._one("SELECT * FROM known WHERE posting_id = ?", posting_id)
        return _known(row) if row else None

    def upsert_known(
        self, posting: Posting, listing_hash: str, last_seen_at: datetime, missing_count: int = 0
    ) -> None:
        """Store a posting as it now is. When it was first seen never changes once stored."""
        with self._tx() as db:
            db.execute(
                """INSERT INTO known (posting_id, source_id, external_id, listing_hash, content_hash,
                    record_json, first_seen_at, last_seen_at, closed_at, missing_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (posting_id) DO UPDATE SET
                    listing_hash = excluded.listing_hash, content_hash = excluded.content_hash,
                    record_json = excluded.record_json, last_seen_at = excluded.last_seen_at,
                    closed_at = excluded.closed_at, missing_count = excluded.missing_count""",
                (
                    posting.id,
                    posting.source_id,
                    posting.external_id,
                    listing_hash,
                    posting.content_hash,
                    json.dumps(posting.to_record(), ensure_ascii=False),
                    stamp(posting.first_seen_at),
                    stamp(last_seen_at),
                    stamp(posting.closed_at),
                    missing_count,
                ),
            )

    def described(self, posting_id: str, at: datetime | None) -> None:
        """When the posting's page was read for its description; None for not yet."""
        with self._tx() as db:
            db.execute("UPDATE known SET described_at = ? WHERE posting_id = ?", (stamp(at), posting_id))

    def tried(self, posting_id: str, at: datetime, most: int) -> None:
        """The posting's page was asked and failed: it waits behind the others, and after `most` tries it's
        left without a description."""
        with self._tx() as db:
            db.execute(
                """UPDATE known SET describe_tries = describe_tries + 1,
                    described_at = CASE WHEN describe_tries + 1 >= ? THEN ? ELSE described_at END
                WHERE posting_id = ?""",
                (most, stamp(at), posting_id),
            )

    def undescribed(self, source_ids: Iterable[str], most: int) -> list[KnownRow]:
        """Up to `most` open postings of these sources whose page hasn't been read yet, the least tried and
        then the newest first."""
        ids = sorted(source_ids)
        marks = ", ".join("?" * len(ids))
        rows = self._all(
            f"""SELECT * FROM known WHERE source_id IN ({marks}) AND closed_at IS NULL
                AND record_json IS NOT NULL AND described_at IS NULL
            ORDER BY describe_tries, first_seen_at DESC, posting_id LIMIT ?""",
            *ids,
            most,
        )
        return [_known(row) for row in rows]

    def seen(self, posting_ids: Iterable[str], now: datetime) -> None:
        """These postings were on their board just now: no longer missing, and kept from pruning."""
        with self._tx() as db:
            db.executemany(
                "UPDATE known SET last_seen_at = ?, missing_count = 0 WHERE posting_id = ?",
                [(stamp(now), posting_id) for posting_id in posting_ids],
            )

    def close_known(self, posting_id: str, closed_at: datetime, missing_count: int) -> None:
        """Close a posting whose record was pruned: its id and hashes are all that's left."""
        with self._tx() as db:
            db.execute(
                "UPDATE known SET closed_at = ?, missing_count = ? WHERE posting_id = ?",
                (stamp(closed_at), missing_count, posting_id),
            )

    def undecided(self, user_id: str, source_ids: Iterable[str], since: datetime) -> Iterator[Posting]:
        """Open postings seen since `since` on these sources that this person has no decision about."""
        sources = list(source_ids)
        if not sources:
            return
        marks = ", ".join("?" for _ in sources)
        rows = self._all(
            f"""SELECT record_json FROM known k WHERE closed_at IS NULL AND record_json IS NOT NULL
            AND last_seen_at >= ? AND source_id IN ({marks})
            AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.user_id = ? AND d.posting_id = k.posting_id)
            ORDER BY first_seen_at, posting_id""",
            stamp(since),
            *sources,
            user_id,
        )
        for row in rows:
            yield Posting.from_record(json.loads(row["record_json"]))

    def set_missing(self, posting_id: str, missing_count: int) -> None:
        """How many checks in a row an open posting has been missing from its board."""
        with self._tx() as db:
            db.execute("UPDATE known SET missing_count = ? WHERE posting_id = ?", (missing_count, posting_id))

    def open_postings(self, source_id: str) -> list[KnownRow]:
        rows = self._all("SELECT * FROM known WHERE source_id = ? AND closed_at IS NULL", source_id)
        return [_known(row) for row in rows]

    def open_copies(self) -> list[Copy]:
        """Every open posting still with its record, as the Repeats rule tells copies of a job apart."""
        rows = self._all(
            """SELECT posting_id, source_id, first_seen_at,
                json_extract(record_json, '$.company') AS company,
                json_extract(record_json, '$.title') AS title,
                json_extract(record_json, '$.location') AS location,
                json_extract(record_json, '$.url') AS url,
                json_extract(record_json, '$.apply_url') AS apply_url
            FROM known WHERE closed_at IS NULL AND record_json IS NOT NULL"""
        )
        return [
            Copy(r["posting_id"], r["source_id"], r["company"] or "", r["title"] or "", r["location"] or "",
                 r["first_seen_at"], links(r["url"] or "", r["apply_url"] or ""))
            for r in rows
        ]  # fmt: skip

    def recent_postings(self, since: datetime) -> Iterator[Posting]:
        """Postings seen since `since`, still with their records, oldest first."""
        rows = self._all(
            "SELECT record_json FROM known WHERE last_seen_at >= ? AND record_json IS NOT NULL "
            "ORDER BY first_seen_at, posting_id",
            stamp(since),
        )
        for row in rows:
            yield Posting.from_record(json.loads(row["record_json"]))

    # What was skipped -----------------------------------------------------------------------------------

    def record_skipped(self, rows: Iterable[SkippedRow]) -> None:
        with self._tx() as db:
            values = [
                (r.source_id, r.external_id, r.company, r.title, r.location, stamp(r.seen_at)) for r in rows
            ]
            db.executemany("INSERT OR REPLACE INTO skipped VALUES (?, ?, ?, ?, ?, ?)", values)

    def skipped_since(self, since: datetime) -> Iterator[SkippedRow]:
        for row in self._all("SELECT * FROM skipped WHERE seen_at >= ? ORDER BY seen_at", stamp(since)):
            yield SkippedRow(
                row["source_id"], row["external_id"], row["company"], row["title"], row["location"],
                must_time(row["seen_at"]),
            )  # fmt: skip

    # GitHub-list rows, for the comparison ---------------------------------------------------------------

    def record_list_rows(self, rows: Iterable[ListRow]) -> None:
        values = [
            (
                r.source_id,
                r.external_id,
                r.company,
                r.title,
                r.url,
                r.alias,
                stamp(r.posted_at),
                stamp(r.seen_at),
            )
            for r in rows
        ]
        with self._tx() as db:
            db.executemany("INSERT OR REPLACE INTO list_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?)", values)

    def list_rows_since(self, since: datetime) -> Iterator[ListRow]:
        """Rows posted (or, without a date, seen) since `since`."""
        rows = self._all(
            "SELECT * FROM list_rows WHERE COALESCE(posted_at, seen_at) >= ? ORDER BY source_id, external_id",
            stamp(since),
        )
        for row in rows:
            yield ListRow(
                row["source_id"], row["external_id"], row["company"], row["title"], row["url"], row["alias"],
                time_or_none(row["posted_at"]), must_time(row["seen_at"]),
            )  # fmt: skip

    def list_dates(self, besides: str) -> Iterator[tuple[str, datetime]]:
        """Each link the lists other than `besides` give, with the date that list gives it."""
        rows = self._all(
            "SELECT url, posted_at FROM list_rows WHERE source_id != ? AND posted_at IS NOT NULL", besides
        )
        for row in rows:
            yield row["url"], must_time(row["posted_at"])

    def short_link(self, short: str) -> str | None:
        """Where a short link was found to lead, or None if it hasn't been asked."""
        row = self._one("SELECT link FROM short_links WHERE short = ?", short)
        return None if row is None else str(row["link"])

    def keep_short_link(self, short: str, link: str, at: datetime) -> None:
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO short_links VALUES (?, ?, ?)", (short, link, stamp(at)))

    def used_short_links(self, shorts: Iterable[str], at: datetime) -> None:
        """Mark the short links a run used, so the ones still listed aren't pruned and asked again."""
        with self._tx() as db:
            db.executemany(
                "UPDATE short_links SET used_at = ? WHERE short = ?", [(stamp(at), s) for s in shorts]
            )

    # How each source is doing ---------------------------------------------------------------------------

    def source_state(self, source_id: str) -> SourceState | None:
        row = self._one("SELECT * FROM source_state WHERE source_id = ?", source_id)
        if row is None:
            return None
        return SourceState(
            source_id=row["source_id"],
            last_checked_at=time_or_none(row["last_checked_at"]),
            last_ok_at=time_or_none(row["last_ok_at"]),
            last_found_at=time_or_none(row["last_found_at"]),
            consecutive_failures=row["consecutive_failures"],
            last_error=row["last_error"],
            open_count=row["open_count"],
        )

    def record_check(
        self, source_id: str, ok: bool, error: str | None, now: datetime, found: int, open_count: int
    ) -> None:
        """One check of a source: a success clears its failures, a failure adds one and keeps its count."""
        at = stamp(now)
        with self._tx() as db:
            db.execute("INSERT OR IGNORE INTO source_state (source_id) VALUES (?)", (source_id,))
            if ok:
                db.execute(
                    """UPDATE source_state SET last_checked_at = ?, last_ok_at = ?, consecutive_failures = 0,
                    last_error = NULL, open_count = ?,
                    last_found_at = CASE WHEN ? > 0 THEN ? ELSE last_found_at END
                    WHERE source_id = ?""",
                    (at, at, open_count, found, at, source_id),
                )
            else:
                db.execute(
                    """UPDATE source_state SET last_checked_at = ?, last_error = ?,
                    consecutive_failures = consecutive_failures + 1 WHERE source_id = ?""",
                    (at, error, source_id),
                )

    # Decisions ----------------------------------------------------------------------------------------------

    def last_decision(self, user_id: str, posting_id: str) -> DecisionRow | None:
        row = self._one("SELECT * FROM decisions WHERE user_id = ? AND posting_id = ?", user_id, posting_id)
        if row is None:
            return None
        return DecisionRow(row["state"], row["rule_id"], row["settings_key"], row["digest"])

    def save_decision(
        self,
        user_id: str,
        posting_id: str,
        state: str,
        rule_id: str | None,
        settings_key: str,
        digest: str,
    ) -> None:
        with self._tx() as db:
            db.execute(
                """INSERT INTO decisions (user_id, posting_id, state, rule_id, settings_key, digest)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id, posting_id) DO UPDATE SET state = excluded.state,
                    rule_id = excluded.rule_id, settings_key = excluded.settings_key,
                    digest = excluded.digest""",
                (user_id, posting_id, state, rule_id, settings_key, digest),
            )

    # The outbox -----------------------------------------------------------------------------------------

    def enqueue(self, kind: OutboxKind, record: dict[str, Any]) -> None:
        """Keep a record to send. It's checked against its schema now, so a bad one never blocks the rest, and
        noted with the contract it was checked under."""
        check_record(kind, record)
        with self._tx() as db:
            db.execute(
                """INSERT INTO outbox (kind, record_json, created_at, contract)
                VALUES (?, ?, datetime('now'), ?)""",
                (kind, json.dumps(record, ensure_ascii=False), contract_digest()),
            )

    def outbox_batches(self, seqs: Collection[int] | None = None) -> Iterator[OutboxBatch]:
        """The outbox as batches to send, or just the records `seqs` names. A batch is `checked` when every
        record in it was checked under this contract; one with a record queued under another is checked
        again as it's sent."""
        rows = self._all("SELECT seq, kind, record_json, contract FROM outbox ORDER BY seq")
        if seqs is not None:
            wanted = set(seqs)
            rows = [row for row in rows if row["seq"] in wanted]
        current = contract_digest()
        stale = {row["seq"] for row in rows if row["contract"] != current}
        found = batches((row["seq"], row["kind"], json.loads(row["record_json"])) for row in rows)
        return (replace(batch, checked=stale.isdisjoint(batch.seqs)) for batch in found)

    def outbox_seqs(self, kind: OutboxKind) -> list[int]:
        """The records of one kind waiting in the outbox, oldest first."""
        return [row["seq"] for row in self._all("SELECT seq FROM outbox WHERE kind = ? ORDER BY seq", kind)]

    def set_aside(self, seqs: Iterable[int], reason: str) -> None:
        """Move records the app refused out of the outbox, with the reason, so the rest can go."""
        marks = [(seq,) for seq in seqs]
        with self._tx() as db:
            db.executemany(
                """INSERT OR REPLACE INTO set_aside (seq, kind, record_json, reason, set_aside_at)
                SELECT seq, kind, record_json, ?, datetime('now') FROM outbox WHERE seq = ?""",
                [(reason[:1000], seq) for (seq,) in marks],
            )
            db.executemany("DELETE FROM outbox WHERE seq = ?", marks)

    def set_aside_news(self) -> tuple[int, str | None]:
        """How many records were set aside since this was last asked, and the first one's reason; they're
        then reported."""
        with self._tx() as db:
            rows = db.execute("SELECT reason FROM set_aside WHERE reported = 0 ORDER BY seq").fetchall()
            db.execute("UPDATE set_aside SET reported = 1 WHERE reported = 0")
        return len(rows), (rows[0][0] if rows else None)

    def set_aside_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM set_aside")
        return int(row["n"]) if row else 0

    def ack(self, seqs: Iterable[int]) -> None:
        with self._tx() as db:
            db.executemany("DELETE FROM outbox WHERE seq = ?", [(seq,) for seq in seqs])

    # Meta and the run lock ------------------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self._one("SELECT value FROM meta WHERE key = ?", key)
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def try_lock(self, owner: str, now: datetime, stale_after: timedelta = timedelta(minutes=15)) -> bool:
        """Take the run lock, unless another owner took it less than `stale_after` ago."""
        with self._tx() as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'lock'").fetchone()
            if row:
                held = json.loads(row["value"])
                if held["owner"] != owner and now - must_time(held["since"]) < stale_after:
                    return False
            lock = json.dumps({"owner": owner, "since": stamp(now)})
            db.execute("INSERT OR REPLACE INTO meta VALUES ('lock', ?)", (lock,))
            return True

    def unlock(self, owner: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM meta WHERE key = 'lock' AND json_extract(value, '$.owner') = ?", (owner,))

    def prune(self, now: datetime, keep_days: int = 30) -> None:
        """Drop records unseen for `keep_days`, keeping their ids and hashes, which decide novelty."""
        cutoff = stamp(now - timedelta(days=keep_days))
        with self._tx() as db:
            db.execute("UPDATE known SET record_json = NULL WHERE last_seen_at < ?", (cutoff,))
            db.execute("DELETE FROM skipped WHERE seen_at < ?", (cutoff,))
            db.execute("DELETE FROM list_rows WHERE seen_at < ?", (cutoff,))
            db.execute("DELETE FROM short_links WHERE used_at < ?", (cutoff,))
            db.execute("DELETE FROM set_aside WHERE julianday(set_aside_at) < julianday(?)", (cutoff,))


def _known(row: sqlite3.Row) -> KnownRow:
    return KnownRow(
        posting_id=row["posting_id"],
        source_id=row["source_id"],
        external_id=row["external_id"],
        listing_hash=row["listing_hash"],
        content_hash=row["content_hash"],
        record_json=row["record_json"],
        first_seen_at=must_time(row["first_seen_at"]),
        last_seen_at=must_time(row["last_seen_at"]),
        closed_at=time_or_none(row["closed_at"]),
        missing_count=row["missing_count"],
        described_at=time_or_none(row["described_at"]),
    )

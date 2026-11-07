"""The local database's tables, built up one migration at a time.

`PRAGMA user_version` holds how many migrations have run. A migration is never edited once released: a change
is a new migration at the end.
"""

from __future__ import annotations

import sqlite3

MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        # Every posting the engine has stored. The ids and hashes stay for good, since they decide whether a
        # listing is new; the record itself is dropped after 30 days unseen.
        """CREATE TABLE known (
            posting_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            external_id TEXT NOT NULL,
            listing_hash TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            record_json TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            closed_at TEXT,
            missing_count INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX known_by_source ON known (source_id, closed_at)",
        "CREATE INDEX known_by_last_seen ON known (last_seen_at)",
        # Listings no one's kinds of role keep: not sent, but kept a while for previews of wider kinds.
        """CREATE TABLE skipped (
            source_id TEXT NOT NULL,
            external_id TEXT NOT NULL,
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            location TEXT NOT NULL,
            seen_at TEXT NOT NULL,
            PRIMARY KEY (source_id, external_id)
        )""",
        "CREATE INDEX skipped_by_seen ON skipped (seen_at)",
        """CREATE TABLE source_state (
            source_id TEXT PRIMARY KEY,
            last_checked_at TEXT,
            last_ok_at TEXT,
            last_found_at TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            open_count INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE decisions (
            user_id TEXT NOT NULL,
            posting_id TEXT NOT NULL,
            state TEXT NOT NULL,
            rule_id TEXT,
            settings_key TEXT NOT NULL,
            PRIMARY KEY (user_id, posting_id)
        )""",
        """CREATE TABLE verdicts (
            posting_id TEXT NOT NULL,
            details_key TEXT NOT NULL,
            ok INTEGER,
            aspect TEXT,
            why TEXT,
            judged_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (posting_id, details_key)
        )""",
        # What's waiting to reach the app, in the order it must arrive.
        """CREATE TABLE outbox (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            record_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    ),
    (
        # The GitHub-list rows someone's kinds of role collect, including the ones left out because jobbot
        # reads their job on its own board: the comparison with the lists counts both.
        """CREATE TABLE list_rows (
            source_id TEXT NOT NULL,
            external_id TEXT NOT NULL,
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            alias TEXT,
            posted_at TEXT,
            seen_at TEXT NOT NULL,
            PRIMARY KEY (source_id, external_id)
        )""",
        "CREATE INDEX list_rows_by_seen ON list_rows (seen_at)",
    ),
    (
        # The contract each queued record was checked under (schema.contract_digest): one queued by an
        # earlier release is checked again before it's sent.
        "ALTER TABLE outbox ADD COLUMN contract TEXT",
    ),
    (
        # Whether a posting's text has a word the eligibility check is asked about (screen/triggers.py),
        # noted as it's stored. NULL for a posting stored before, which is searched when it's needed.
        "ALTER TABLE known ADD COLUMN has_trigger INTEGER",
    ),
    (
        # Outbox records the app refused, kept 30 days with its reason so they can be looked at; the next
        # run names them in its problems, and marks them reported.
        """CREATE TABLE set_aside (
            seq INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            record_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            set_aside_at TEXT NOT NULL,
            reported INTEGER NOT NULL DEFAULT 0
        )""",
    ),
    (
        # A digest of the decision last sent (its state, rule, values, notes and tags), so a change in any of
        # them is sent. NULL for a decision stored before, which is sent again once.
        "ALTER TABLE decisions ADD COLUMN digest TEXT",
    ),
    (
        # Each person's tags for a posting as last read (rules/tags.py's read_tags), and a digest of what they
        # were read from, so judging again after a change of what's hidden doesn't read them again.
        """CREATE TABLE tags (
            user_id TEXT NOT NULL,
            posting_id TEXT NOT NULL,
            basis TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            PRIMARY KEY (user_id, posting_id)
        )""",
        "CREATE INDEX tags_by_posting ON tags (posting_id)",
    ),
    (
        # What the app holds for the decision as last sent (scheduler/decide.py's counted_form), which the tag
        # counts are counted from. NULL until one is sent, or for a decision stored before, judged again once.
        "ALTER TABLE decisions ADD COLUMN counted TEXT",
    ),
    (
        # Plain rules in place of tags and the eligibility check (decision record 0020): what was kept for
        # them goes, with any tag counts still waiting to send, which the app no longer takes.
        "DROP TABLE tags",
        "DROP TABLE verdicts",
        "ALTER TABLE decisions DROP COLUMN counted",
        "ALTER TABLE known DROP COLUMN has_trigger",
        "DELETE FROM meta WHERE key = 'trigger_words'",
        "DELETE FROM outbox WHERE kind = 'tag_counts'",
        "DELETE FROM set_aside WHERE kind = 'tag_counts'",
        # A decision queued before carries its tags, which the app no longer takes: it goes without them.
        "UPDATE outbox SET record_json = json_remove(record_json, '$.tags') WHERE kind = 'decision'",
    ),
    (
        # When a GitHub-list posting's page was read for its description (sources/describe.py). NULL until
        # it's tried: the postings stored before are described later, a few each run. A page that keeps
        # failing is counted, so it waits behind the others and is given up on at last.
        "ALTER TABLE known ADD COLUMN described_at TEXT",
        "ALTER TABLE known ADD COLUMN describe_tries INTEGER NOT NULL DEFAULT 0",
    ),
    (
        # Where a list's short links lead (sources/github_list.py SHORT_LINK_HOSTS), asked once each, and
        # when a run last used one: a link no longer listed goes with the list's other old rows.
        """CREATE TABLE short_links (
            short TEXT PRIMARY KEY,
            link TEXT NOT NULL,
            used_at TEXT NOT NULL
        )""",
    ),
    (
        # Fitzii pages were read before jobbot knew where they keep the description (sources/describe.py):
        # the open ones that came without one, or with only the list's sponsorship line, are read again.
        """UPDATE known SET described_at = NULL, describe_tries = 0
        WHERE closed_at IS NULL AND record_json IS NOT NULL
            AND json_extract(record_json, '$.description') IN (
                '', 'Sponsorship: Does Not Offer Sponsorship', 'Sponsorship: U.S. Citizenship is Required')
            AND (json_extract(record_json, '$.url') LIKE 'https://fitzii.com/%'
                OR json_extract(record_json, '$.url') LIKE 'https://www.fitzii.com/%')""",
    ),
    (
        # The same for TikTok, ByteDance, Lever's apply pages, amazon.jobs and SuccessFactors career sites
        # (sources/describe.py), which came without a description until jobbot knew where each keeps it.
        """UPDATE known SET described_at = NULL, describe_tries = 0
        WHERE closed_at IS NULL AND record_json IS NOT NULL
            AND json_extract(record_json, '$.description') IN (
                '', 'Sponsorship: Does Not Offer Sponsorship', 'Sponsorship: U.S. Citizenship is Required')
            AND (json_extract(record_json, '$.url') LIKE 'https://lifeattiktok.com/%'
                OR json_extract(record_json, '$.url') LIKE 'https://jobs.bytedance.com/%'
                OR json_extract(record_json, '$.url') LIKE 'https://joinbytedance.com/%'
                OR json_extract(record_json, '$.url') LIKE 'https://jobs.lever.co/%'
                OR json_extract(record_json, '$.url') LIKE 'https://jobs.eu.lever.co/%'
                OR json_extract(record_json, '$.url') LIKE 'https://amazon.jobs/%'
                OR json_extract(record_json, '$.url') LIKE 'https://www.amazon.jobs/%'
                OR json_extract(record_json, '$.url') LIKE 'https://%ats=successfactors%')""",
    ),
    (
        # The Simplify list gave NVIDIA's Santa Clara jobs "Canada", which only the job's own Workday record
        # corrects (places.keep_countries). The open Workday list postings that may name a country alone,
        # those giving several places or one without a comma, are read again with their record.
        """UPDATE known SET described_at = NULL, describe_tries = 0
        WHERE closed_at IS NULL AND record_json IS NOT NULL AND source_id LIKE 'github\\_list:%' ESCAPE '\\'
            AND (json_extract(record_json, '$.url') LIKE 'https://%.myworkdayjobs.com/%'
                OR json_extract(record_json, '$.url') LIKE 'https://%.myworkdaysite.com/%')
            AND (json_extract(record_json, '$.location') LIKE '%;%'
                OR json_extract(record_json, '$.location') NOT LIKE '%,%')""",
    ),
)


def migrate(db: sqlite3.Connection) -> None:
    """Run the migrations this database hasn't had yet, each in its own transaction.

    Which migrations are done is read inside each one's write transaction, so two processes opening a new
    database at once (the run and the jobs service) don't both run the same one.
    """
    while True:
        db.execute("BEGIN IMMEDIATE")
        try:
            done: int = db.execute("PRAGMA user_version").fetchone()[0]
            if done >= len(MIGRATIONS):
                db.execute("COMMIT")
                return
            for statement in MIGRATIONS[done]:
                db.execute(statement)
            db.execute(f"PRAGMA user_version = {done + 1}")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

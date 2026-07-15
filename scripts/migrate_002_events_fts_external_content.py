# /// script
# requires-python = ">=3.11"
# ///
"""One-time migration: convert events_fts to external-content FTS5.

events_fts was created standalone, and insert_event used INSERT OR REPLACE.
FTS5 tables have no PRIMARY KEY, so OR REPLACE degraded to a plain INSERT:
every re-synced event silently duplicated its FTS row. Live DBs are clean
today only because nothing has re-synced yet. Backfill re-syncs by design.

Converts to content=events, content_rowid=rowid with sync triggers, then
rebuilds from the existing rows. Safe: no data lives only in events_fts;
every indexed value is reconstructable from events.content.

Idempotent: re-running detects external content is already in place and exits.

Usage:
    uv run scripts/migrate_002_events_fts_external_content.py --dry-run
    uv run scripts/migrate_002_events_fts_external_content.py --all
"""

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

LOGGING_ROOT = Path.home() / ".claude" / "local" / "logging"

FTS_DDL = """
CREATE VIRTUAL TABLE events_fts USING fts5(
    content,
    content=events,
    content_rowid=rowid,
    tokenize='porter'
);
CREATE TRIGGER IF NOT EXISTS events_fts_ai AFTER INSERT ON events
WHEN new.content IS NOT NULL AND new.content != ''
BEGIN
    INSERT INTO events_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS events_fts_ad AFTER DELETE ON events
WHEN old.content IS NOT NULL AND old.content != ''
BEGIN
    INSERT INTO events_fts(events_fts, rowid, content) VALUES('delete', old.rowid, old.content);
END;
-- ONE body: split AFTER UPDATE triggers fire in reverse creation order and
-- the delete would win, silently dropping the row from the index.
CREATE TRIGGER IF NOT EXISTS events_fts_au AFTER UPDATE ON events
BEGIN
    INSERT INTO events_fts(events_fts, rowid, content)
        SELECT 'delete', old.rowid, old.content
        WHERE old.content IS NOT NULL AND old.content != '';
    INSERT INTO events_fts(rowid, content)
        SELECT new.rowid, new.content
        WHERE new.content IS NOT NULL AND new.content != '';
END;
"""


def is_external_content(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='events_fts'").fetchone()
    return bool(row) and "content=events" in row[0].replace("'", "").replace(" ", "")


def migrate(db_path: Path, dry_run: bool) -> str:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='events_fts'").fetchone():
            return "skip (no events_fts)"
        if is_external_content(conn):
            return "skip (already external content)"

        expected = conn.execute("SELECT COUNT(*) FROM events WHERE content IS NOT NULL AND content != ''").fetchone()[0]
        if dry_run:
            return f"would migrate ({expected} rows to index)"

        backup = db_path.with_suffix(db_path.suffix + ".pre-migrate002")
        shutil.copy2(db_path, backup)

        conn.executescript(
            # Drop every events_fts trigger first. CREATE TRIGGER IF NOT EXISTS
            # will NOT replace stale ones (e.g. the interim split au_del/au_ins
            # pair), and leftover triggers referencing the old table shape raise
            # "SQL logic error" on every insert.
            "DROP TRIGGER IF EXISTS events_fts_ai;"
            "DROP TRIGGER IF EXISTS events_fts_ad;"
            "DROP TRIGGER IF EXISTS events_fts_au;"
            "DROP TRIGGER IF EXISTS events_fts_au_del;"
            "DROP TRIGGER IF EXISTS events_fts_au_ins;"
            "DROP TABLE events_fts;" + FTS_DDL
        )
        conn.execute("INSERT INTO events_fts(events_fts) VALUES('rebuild')")
        conn.execute("INSERT INTO events_fts(events_fts) VALUES('integrity-check')")
        conn.commit()

        indexed = conn.execute("SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'e OR a OR i'").fetchone()[0]
        return (
            f"migrated ({expected} rows expected, index rebuilt, integrity OK, "
            f"backup {backup.name}, sample match {indexed})"
        )
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true", help="migrate every per-project DB")
    ap.add_argument("--db", help="migrate a single DB path")
    args = ap.parse_args()

    if args.db:
        dbs = [Path(args.db)]
    elif args.all:
        dbs = sorted(LOGGING_ROOT.glob("*/db/logging.db"))
    else:
        print("specify --all or --db PATH")
        return 2

    for db in dbs:
        print(f"{db}: {migrate(db, args.dry_run)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

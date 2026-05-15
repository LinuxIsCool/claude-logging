#!/usr/bin/env python3
"""task-508 Phase 1.1 — initialize cross-project index DB.

Creates ~/.claude/local/logging/_index/index.db with schema from
init_cross_project_index.sql. Idempotent: safe to re-run; uses
CREATE TABLE IF NOT EXISTS.

After this runs, rollup_index.py (Phase 1.5) populates events_index from
per-project DBs every 60s.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

INDEX_DIR = Path.home() / ".claude" / "local" / "logging" / "_index"
INDEX_DB = INDEX_DIR / "index.db"
SCRIPTS_DIR = Path(__file__).parent
SCHEMA = SCRIPTS_DIR / "init_cross_project_index.sql"


def main() -> int:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Index DB target: {INDEX_DB}")

    con = sqlite3.connect(INDEX_DB)
    sql = SCHEMA.read_text()

    # Strip comment-only lines and run as a script (CREATE TABLE IF NOT EXISTS
    # makes this safe for idempotent re-runs)
    script = "\n".join(
        line for line in sql.split("\n") if not line.strip().startswith("--")
    )
    con.executescript(script)
    con.commit()

    # Verify — exclude FTS5 shadow tables (events_index_fts_data, _idx, _docsize, etc.)
    all_tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND (sql IS NULL OR sql NOT LIKE '%USING fts5%') ORDER BY name"
    ).fetchall()]
    fts_suffixes = ("_data", "_idx", "_docsize", "_content", "_config")
    tables = [t for t in all_tables if not t.endswith(fts_suffixes)]
    fts_tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%USING fts5%'"
    ).fetchall()]

    expected_real = {"events_index", "rollup_state", "hostnames"}
    expected_fts = {"events_index_fts"}

    assert set(tables) == expected_real, f"tables mismatch: {set(tables) ^ expected_real}"
    assert set(fts_tables) == expected_fts, f"fts tables mismatch: {set(fts_tables) ^ expected_fts}"

    rollup_count = con.execute("SELECT COUNT(*) FROM rollup_state").fetchone()[0]
    events_idx_count = con.execute("SELECT COUNT(*) FROM events_index").fetchone()[0]

    print(f"✓ {INDEX_DB} ready")
    print(f"  Tables:           {', '.join(tables)}")
    print(f"  FTS5 mirrors:     {', '.join(fts_tables)}")
    print(f"  rollup_state rows: {rollup_count}")
    print(f"  events_index rows: {events_idx_count}")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

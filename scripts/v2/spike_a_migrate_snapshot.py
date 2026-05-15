#!/usr/bin/env python3
"""Spike A — task-508 Phase 0.

Applies migrate_001_add_columns_and_tables.sql to a snapshot of the largest
live project DB. Verifies the additive-only contract via 8 invariants:

  1. Session count unchanged.
  2. Event count unchanged.
  3. Events FTS count unchanged.
  4. Event type distribution unchanged (per-type counts identical).
  5. Exactly 8 new columns added to events; names match expected.
  6. 4 new tables created (prompts, annotations, pastes, tool_calls).
  7. prompts_fts FTS5 virtual table created.
  8. Idempotent re-run does NOT corrupt schema or alter counts.

Plus the cross-project index DB scaffold (init_cross_project_index.sql)
applied to /tmp/logging-spike-a/index.db.

Exit code 0 on success; non-zero with assertion message on any failure.

Runs against /tmp/logging-spike-a/logging.db.snapshot (a copy). Live data
NEVER touched.
"""
import sqlite3
import sys
from pathlib import Path

SNAPSHOT = Path("/tmp/logging-spike-a/logging.db.snapshot")
INDEX_DB = Path("/tmp/logging-spike-a/index.db")
SCRIPTS_DIR = Path(__file__).parent
MIGRATION = SCRIPTS_DIR / "migrate_001_add_columns_and_tables.sql"
INDEX_SCHEMA = SCRIPTS_DIR / "init_cross_project_index.sql"

EXPECTED_NEW_COLUMNS = {
    "persona", "agent_id", "tool_name", "tool_input_hash",
    "duration_ms", "tokens_in", "tokens_out", "cost_usd",
}
EXPECTED_NEW_TABLES = {"prompts", "annotations", "pastes", "tool_calls"}


def apply_migration(con: sqlite3.Connection, sql_text: str, label: str) -> int:
    """Apply migration; tolerate idempotent re-runs.

    Strategy:
      1. ALTER TABLE ADD COLUMN statements (need duplicate-tolerance) — run
         individually with except-on-duplicate.
      2. CREATE TABLE / CREATE INDEX / CREATE VIRTUAL TABLE — already use
         IF NOT EXISTS so executescript() is safe + atomic.
    """
    skipped = 0
    # Strip comment-only lines globally before parsing
    comment_stripped_lines = []
    for line in sql_text.split("\n"):
        if line.strip().startswith("--"):
            continue
        comment_stripped_lines.append(line)
    sql_no_comments = "\n".join(comment_stripped_lines)

    # Phase 1: ALTER TABLE statements (dup-tolerant)
    alter_stmts = []
    other_stmts = []
    for stmt in sql_no_comments.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        if stmt.upper().startswith("ALTER TABLE"):
            alter_stmts.append(stmt)
        else:
            other_stmts.append(stmt)

    for stmt in alter_stmts:
        try:
            con.execute(stmt)
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "duplicate column name" in msg or "already exists" in msg:
                skipped += 1
                continue
            print(f"  [{label}] ALTER ERROR:\n    {stmt[:160]}\n    -> {e}")
            raise
    con.commit()

    # Phase 2: rest as one executescript (uses IF NOT EXISTS for idempotence)
    if other_stmts:
        script = ";\n".join(other_stmts) + ";"
        try:
            con.executescript(script)
        except sqlite3.OperationalError as e:
            print(f"  [{label}] SCRIPT ERROR: {e}")
            raise
    con.commit()
    return skipped


def main() -> int:
    if not SNAPSHOT.exists():
        print(f"FAIL: snapshot missing at {SNAPSHOT}")
        return 1
    if not MIGRATION.exists():
        print(f"FAIL: migration script missing at {MIGRATION}")
        return 1
    if not INDEX_SCHEMA.exists():
        print(f"FAIL: index schema missing at {INDEX_SCHEMA}")
        return 1

    # --- Part 1: per-project DB migration ---
    con = sqlite3.connect(SNAPSHOT)

    # PRE-snapshot counts + structure
    pre_sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    pre_events = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    pre_events_fts = con.execute("SELECT COUNT(*) FROM events_fts").fetchone()[0]
    pre_event_types = dict(con.execute(
        "SELECT type, COUNT(*) FROM events GROUP BY type"
    ).fetchall())
    pre_columns = [r[1] for r in con.execute("PRAGMA table_info(events)").fetchall()]
    pre_tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}

    print(f"PRE  — sessions={pre_sessions:,}, events={pre_events:,}, "
          f"events_fts={pre_events_fts:,}, columns={len(pre_columns)}, tables={len(pre_tables)}")

    # Apply migration
    sql = MIGRATION.read_text()
    skipped = apply_migration(con, sql, "migrate_001")
    print(f"  applied migrate_001 (skipped {skipped} idempotent statements)")

    # POST-migration counts
    post_sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    post_events = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    post_events_fts = con.execute("SELECT COUNT(*) FROM events_fts").fetchone()[0]
    post_event_types = dict(con.execute(
        "SELECT type, COUNT(*) FROM events GROUP BY type"
    ).fetchall())
    post_columns = [r[1] for r in con.execute("PRAGMA table_info(events)").fetchall()]
    post_tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}

    print(f"POST — sessions={post_sessions:,}, events={post_events:,}, "
          f"events_fts={post_events_fts:,}, columns={len(post_columns)}, tables={len(post_tables)}")

    # INVARIANTS
    assert pre_sessions == post_sessions, \
        f"INV1 FAIL: session count drift {pre_sessions} -> {post_sessions}"
    assert pre_events == post_events, \
        f"INV2 FAIL: event count drift {pre_events} -> {post_events}"
    assert pre_events_fts == post_events_fts, \
        f"INV3 FAIL: events_fts count drift {pre_events_fts} -> {post_events_fts}"
    assert pre_event_types == post_event_types, \
        f"INV4 FAIL: event type distribution drifted (missing/added keys: " \
        f"{set(pre_event_types) ^ set(post_event_types)})"

    new_cols = set(post_columns) - set(pre_columns)
    assert new_cols == EXPECTED_NEW_COLUMNS, \
        f"INV5 FAIL: expected new columns {EXPECTED_NEW_COLUMNS}; got {new_cols}; " \
        f"diff: {new_cols ^ EXPECTED_NEW_COLUMNS}"

    new_tables = post_tables - pre_tables
    # filter out FTS internals (events_fts_*, prompts_fts_*) — only real tables count
    real_new_tables = {t for t in new_tables if not t.endswith(("_data", "_idx", "_docsize", "_content", "_config"))}
    real_new_tables -= {"prompts_fts"}
    assert real_new_tables == EXPECTED_NEW_TABLES, \
        f"INV6 FAIL: expected new tables {EXPECTED_NEW_TABLES}; got {real_new_tables}; " \
        f"diff: {real_new_tables ^ EXPECTED_NEW_TABLES}"

    fts = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='prompts_fts'"
    ).fetchone()
    assert fts is not None, "INV7 FAIL: prompts_fts virtual table missing"

    print("  invariants 1-7 ✓")

    # INV8 — idempotent re-run
    skipped_rerun = apply_migration(con, sql, "migrate_001 (re-run)")
    rerun_events = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    rerun_sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    rerun_columns = [r[1] for r in con.execute("PRAGMA table_info(events)").fetchall()]
    assert rerun_events == post_events, \
        f"INV8a FAIL: re-run changed event count {post_events} -> {rerun_events}"
    assert rerun_sessions == post_sessions, \
        f"INV8b FAIL: re-run changed session count {post_sessions} -> {rerun_sessions}"
    assert len(rerun_columns) == len(post_columns), \
        f"INV8c FAIL: re-run added more columns {len(post_columns)} -> {len(rerun_columns)}"
    assert skipped_rerun > 0, \
        f"INV8d FAIL: re-run should have skipped duplicates; skipped count {skipped_rerun}"

    print(f"  invariant 8 ✓ (re-run skipped {skipped_rerun} duplicates, counts stable)")

    con.close()

    # --- Part 2: cross-project index DB scaffold ---
    if INDEX_DB.exists():
        INDEX_DB.unlink()
    idx_con = sqlite3.connect(INDEX_DB)
    idx_sql = INDEX_SCHEMA.read_text()
    apply_migration(idx_con, idx_sql, "init_cross_project_index")

    # Exclude FTS5 virtual tables + their shadow tables from the real-table count.
    idx_tables = {r[0] for r in idx_con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND (sql IS NULL OR sql NOT LIKE '%USING fts5%')"
    ).fetchall()}
    real_idx_tables = {t for t in idx_tables if not t.endswith(
        ("_data", "_idx", "_docsize", "_content", "_config")
    )}
    expected_idx = {"events_index", "rollup_state", "hostnames"}
    assert real_idx_tables == expected_idx, \
        f"INV9 FAIL: index DB tables mismatch — expected {expected_idx}, got {real_idx_tables}"

    fts_idx = idx_con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='events_index_fts'"
    ).fetchone()
    assert fts_idx is not None, "INV10 FAIL: events_index_fts virtual table missing"

    print(f"  cross-project index DB tables ✓ ({len(real_idx_tables)} real + events_index_fts)")
    idx_con.close()

    print("")
    print("✓ Spike A PASSES — schema migration is additive, lossless, idempotent.")
    print("  Production DBs untouched. /tmp/logging-spike-a/ holds the snapshot artifacts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

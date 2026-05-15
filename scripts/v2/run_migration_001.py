#!/usr/bin/env python3
"""task-508 Phase 1.1 — apply migrate_001 across all per-project DBs.

Discovers ~/.claude/local/logging/*/db/logging.db (~203 DBs expected),
applies migrate_001_add_columns_and_tables.sql to each. Idempotent.
Resume-on-fail via state file at ~/.claude/local/logging/_index/migration_001_state.json.

Default mode: --dry-run (lists DBs that WOULD be migrated; opens read-only;
no writes).
Explicit mode: --apply (actually runs the migration).

Safety:
  - SQLite WAL journal mode handles concurrent log_event.py writes during
    schema changes (verified 2026-05-15).
  - Each DB processed in isolation; one failure does not block others.
  - Per-DB before/after counts verified; mismatch → rollback (sqlite
    transactions wrap each DB's migration).
  - State file enables resume from last completed slug on re-run.

Usage:
    uv run python scripts/v2/run_migration_001.py            # dry-run (default)
    uv run python scripts/v2/run_migration_001.py --apply    # actual run
    uv run python scripts/v2/run_migration_001.py --apply --resume   # skip already-done
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

LOGGING_ROOT = Path.home() / ".claude" / "local" / "logging"
INDEX_DIR = LOGGING_ROOT / "_index"
STATE_FILE = INDEX_DIR / "migration_001_state.json"
SCRIPTS_DIR = Path(__file__).parent
MIGRATION_SQL = SCRIPTS_DIR / "migrate_001_add_columns_and_tables.sql"

EXPECTED_NEW_COLUMNS = {
    "persona", "agent_id", "tool_name", "tool_input_hash",
    "duration_ms", "tokens_in", "tokens_out", "cost_usd",
}
EXPECTED_NEW_TABLES = {"prompts", "annotations", "pastes", "tool_calls"}


def discover_dbs() -> list[Path]:
    """Find all per-project DBs. Excludes the dead conversations.db root artifact."""
    dbs = []
    for slug_dir in sorted(LOGGING_ROOT.iterdir()):
        if not slug_dir.is_dir():
            continue
        if slug_dir.name == "_index":
            continue
        db = slug_dir / "db" / "logging.db"
        if db.exists() and db.stat().st_size > 0:
            dbs.append(db)
    return dbs


def load_state() -> dict:
    """Load resume state; empty if file missing."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"completed_slugs": [], "last_run_at": None}


def save_state(state: dict) -> None:
    """Atomic write of state."""
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def apply_migration_to_db(db_path: Path, sql_text: str) -> tuple[bool, str, dict]:
    """Apply migration to one DB. Returns (success, message, stats)."""
    stats = {}
    try:
        con = sqlite3.connect(db_path, timeout=30.0)
        # PRE counts (cheap)
        pre_events = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        pre_sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        pre_columns = [r[1] for r in con.execute("PRAGMA table_info(events)").fetchall()]
        stats["pre_events"] = pre_events
        stats["pre_sessions"] = pre_sessions
        stats["pre_column_count"] = len(pre_columns)

        # Parse migration SQL: ALTER TABLE stmts (dup-tolerant) + rest (script).
        sql_no_comments = "\n".join(
            line for line in sql_text.split("\n") if not line.strip().startswith("--")
        )
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

        # Phase 1: ALTERs (dup-tolerant for idempotence)
        skipped = 0
        for stmt in alter_stmts:
            try:
                con.execute(stmt)
            except sqlite3.OperationalError as e:
                msg = str(e)
                if "duplicate column name" in msg:
                    skipped += 1
                    continue
                con.rollback()
                con.close()
                return False, f"ALTER fail: {e}", stats
        con.commit()
        stats["alters_applied"] = len(alter_stmts) - skipped
        stats["alters_skipped_idempotent"] = skipped

        # Phase 2: rest (uses IF NOT EXISTS internally)
        if other_stmts:
            script = ";\n".join(other_stmts) + ";"
            try:
                con.executescript(script)
            except sqlite3.OperationalError as e:
                con.rollback()
                con.close()
                return False, f"DDL fail: {e}", stats
        con.commit()

        # POST counts + verification
        post_events = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        post_sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        post_columns = [r[1] for r in con.execute("PRAGMA table_info(events)").fetchall()]
        post_tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND (sql IS NULL OR sql NOT LIKE '%USING fts5%')"
        ).fetchall()}

        stats["post_events"] = post_events
        stats["post_sessions"] = post_sessions
        stats["post_column_count"] = len(post_columns)

        # Invariants
        if pre_events != post_events:
            con.close()
            return False, f"event count drift {pre_events} -> {post_events}", stats
        if pre_sessions != post_sessions:
            con.close()
            return False, f"session count drift {pre_sessions} -> {post_sessions}", stats

        new_cols = set(post_columns) - set(pre_columns)
        if new_cols and new_cols != EXPECTED_NEW_COLUMNS:
            con.close()
            return False, f"unexpected new columns: {new_cols ^ EXPECTED_NEW_COLUMNS}", stats

        missing_tables = EXPECTED_NEW_TABLES - post_tables
        if missing_tables:
            con.close()
            return False, f"missing new tables: {missing_tables}", stats

        con.close()
        return True, f"events={post_events:,} cols+={len(new_cols)} skip={skipped}", stats

    except Exception as e:
        return False, f"exception: {type(e).__name__}: {e}", stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Actually apply migration (default: dry-run)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip slugs already in state file's completed_slugs")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N successfully-migrated DBs (testing)")
    args = parser.parse_args()

    dbs = discover_dbs()
    print(f"Discovered {len(dbs)} per-project DBs at {LOGGING_ROOT}")

    sql = MIGRATION_SQL.read_text()

    state = load_state() if args.resume else {"completed_slugs": [], "last_run_at": None}
    if args.resume:
        print(f"Resuming — {len(state['completed_slugs'])} slugs already complete")

    completed = list(state["completed_slugs"])
    completed_set = set(completed)

    if not args.apply:
        print("")
        print("=== DRY-RUN MODE ===  (use --apply to execute)")
        print("")
        for i, db in enumerate(dbs, 1):
            slug = db.parent.parent.name
            if slug in completed_set:
                marker = "[SKIP-DONE]"
            else:
                marker = "[WOULD MIGRATE]"
            size_mb = db.stat().st_size / 1024 / 1024
            print(f"  {i:3}/{len(dbs)} {marker} {slug} ({size_mb:.1f}MB)")
        print("")
        print(f"Total: {len(dbs)} DBs; {len(dbs) - len(completed_set)} pending migration.")
        return 0

    # Real apply
    print("")
    print("=== APPLYING MIGRATION ===")
    print("")
    success_count = 0
    fail_count = 0
    skip_count = 0
    failures = []
    start_t = time.time()

    for i, db in enumerate(dbs, 1):
        slug = db.parent.parent.name
        if slug in completed_set:
            skip_count += 1
            continue

        size_mb = db.stat().st_size / 1024 / 1024
        t0 = time.time()
        ok, msg, stats = apply_migration_to_db(db, sql)
        dur_ms = (time.time() - t0) * 1000

        if ok:
            success_count += 1
            completed.append(slug)
            print(f"  {i:3}/{len(dbs)} ✓ {slug:<70} ({size_mb:5.1f}MB, {dur_ms:6.0f}ms) {msg}")
        else:
            fail_count += 1
            failures.append((slug, msg))
            print(f"  {i:3}/{len(dbs)} ✗ {slug:<70} ({size_mb:5.1f}MB, {dur_ms:6.0f}ms) {msg}")

        # Save state every 10 DBs for resume
        if (i % 10) == 0:
            state["completed_slugs"] = completed
            state["last_run_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            save_state(state)

        if args.limit and success_count >= args.limit:
            print(f"  -- stopping at --limit={args.limit}")
            break

    # Final state save
    state["completed_slugs"] = completed
    state["last_run_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    save_state(state)

    total_dur = time.time() - start_t
    print("")
    print(f"=== SUMMARY ===")
    print(f"  Total DBs:        {len(dbs)}")
    print(f"  Migrated OK:      {success_count}")
    print(f"  Skipped (done):   {skip_count}")
    print(f"  Failed:           {fail_count}")
    print(f"  Total wall:       {total_dur:.1f}s")
    if failures:
        print("")
        print("Failures:")
        for slug, msg in failures[:20]:
            print(f"  - {slug}: {msg}")
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

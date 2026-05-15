#!/usr/bin/env python3
"""task-508 Phase 1.5 — cross-project rollup into _index/index.db.

Reads new events from each per-project DB at
~/.claude/local/logging/<slug>/db/logging.db and inserts them into the
central index DB at ~/.claude/local/logging/_index/index.db.

Incremental: tracks last_event_ts per project in the `rollup_state` table.
On each run, only events newer than last_event_ts are pulled. The
events_index_fts FTS5 mirror is kept in sync.

Designed to run every 60s via claude-rhythms scheduler. Single-pass; exits
after each run.

Idempotent: re-running before next event arrives produces zero updates.
INSERT OR REPLACE on event_id means existing rows are refreshed if the
underlying event was REPLACED (rare; happens on idempotent hook retry).

Usage:
    uv run python scripts/v2/rollup_index.py              # full run
    uv run python scripts/v2/rollup_index.py --reset      # truncate + full rebuild
    uv run python scripts/v2/rollup_index.py --limit 10   # first 10 DBs only (testing)
"""
from __future__ import annotations

import argparse
import os
import platform
import sqlite3
import sys
import time
from pathlib import Path

LOGGING_ROOT = Path.home() / ".claude" / "local" / "logging"
INDEX_DB = LOGGING_ROOT / "_index" / "index.db"
HOSTNAME = platform.node()

CONTENT_PREVIEW_LEN = 200


def discover_dbs() -> list[tuple[str, Path]]:
    """Return (slug, db_path) pairs."""
    out = []
    for slug_dir in sorted(LOGGING_ROOT.iterdir()):
        if not slug_dir.is_dir() or slug_dir.name == "_index":
            continue
        db = slug_dir / "db" / "logging.db"
        if db.exists() and db.stat().st_size > 0:
            out.append((slug_dir.name, db))
    return out


def get_last_synced_ts(idx_con: sqlite3.Connection, project_slug: str) -> str | None:
    row = idx_con.execute(
        "SELECT last_event_ts FROM rollup_state WHERE project_slug = ?",
        (project_slug,),
    ).fetchone()
    return row[0] if row else None


def rollup_project(idx_con: sqlite3.Connection, slug: str, db_path: Path) -> tuple[int, str | None]:
    """Roll up new events from one project into the index. Returns (inserted, max_ts)."""
    last_ts = get_last_synced_ts(idx_con, slug)
    proj_con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    proj_con.row_factory = sqlite3.Row

    if last_ts:
        cursor = proj_con.execute(
            "SELECT id, session_id, type, ts, persona, content FROM events "
            "WHERE ts > ? ORDER BY ts ASC",
            (last_ts,),
        )
    else:
        cursor = proj_con.execute(
            "SELECT id, session_id, type, ts, persona, content FROM events ORDER BY ts ASC"
        )

    new_rows = []
    fts_rows = []
    max_ts = last_ts
    for row in cursor:
        content_preview = (row["content"] or "")[:CONTENT_PREVIEW_LEN]
        has_full = 1 if (row["content"] and len(row["content"]) > CONTENT_PREVIEW_LEN) else 0
        new_rows.append((
            row["id"], slug, row["session_id"], row["type"], row["ts"],
            row["persona"], content_preview, has_full,
        ))
        fts_rows.append((
            row["id"], slug, row["session_id"], row["type"],
            row["persona"] or "", content_preview,
        ))
        if max_ts is None or row["ts"] > max_ts:
            max_ts = row["ts"]

    proj_con.close()

    if not new_rows:
        return 0, max_ts

    idx_con.executemany(
        "INSERT OR REPLACE INTO events_index "
        "(event_id, project_slug, session_id, type, ts, persona, content_preview, has_full_content) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        new_rows,
    )
    idx_con.executemany(
        "INSERT OR REPLACE INTO events_index_fts "
        "(event_id, project_slug, session_id, type, persona, content_preview) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        fts_rows,
    )
    return len(new_rows), max_ts


def update_rollup_state(idx_con: sqlite3.Connection, slug: str, last_ts: str | None, event_count: int) -> None:
    idx_con.execute(
        "INSERT INTO rollup_state (project_slug, last_event_ts, last_synced_at, event_count, schema_version) "
        "VALUES (?, ?, CURRENT_TIMESTAMP, ?, 1) "
        "ON CONFLICT(project_slug) DO UPDATE SET "
        "last_event_ts = excluded.last_event_ts, "
        "last_synced_at = excluded.last_synced_at, "
        "event_count = excluded.event_count",
        (slug, last_ts, event_count),
    )


def update_hostname_state(idx_con: sqlite3.Connection, hostname: str, project_count: int, last_event_ts: str | None) -> None:
    """Record this machine's hostname + project count (Q5 multi-machine merge prep)."""
    idx_con.execute(
        "INSERT INTO hostnames (hostname, first_seen, last_event_ts, project_count) "
        "VALUES (?, CURRENT_TIMESTAMP, ?, ?) "
        "ON CONFLICT(hostname) DO UPDATE SET "
        "last_event_ts = excluded.last_event_ts, "
        "project_count = excluded.project_count",
        (hostname, last_event_ts, project_count),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="Truncate events_index + rollup_state before run (full rebuild)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only N DBs (testing)")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress per-project lines; print summary only")
    args = parser.parse_args()

    if not INDEX_DB.exists():
        print(f"ERROR: index DB missing at {INDEX_DB}. Run init_cross_project_index_db.py first.")
        return 1

    idx_con = sqlite3.connect(INDEX_DB, timeout=30.0)

    if args.reset:
        print("RESET: truncating events_index, events_index_fts, rollup_state")
        idx_con.execute("DELETE FROM events_index")
        idx_con.execute("DELETE FROM events_index_fts")
        idx_con.execute("DELETE FROM rollup_state")
        idx_con.commit()

    dbs = discover_dbs()
    if args.limit:
        dbs = dbs[:args.limit]

    total_inserted = 0
    global_max_ts: str | None = None
    start_t = time.time()

    for i, (slug, db) in enumerate(dbs, 1):
        try:
            t0 = time.time()
            inserted, max_ts = rollup_project(idx_con, slug, db)
            update_rollup_state(idx_con, slug, max_ts, inserted)
            dur_ms = (time.time() - t0) * 1000

            if not args.quiet:
                if inserted > 0:
                    print(f"  {i:3}/{len(dbs)} +{inserted:6,} {slug:<70} ({dur_ms:5.0f}ms)")
                # silent on zero-update projects in default mode

            total_inserted += inserted
            if max_ts and (global_max_ts is None or max_ts > global_max_ts):
                global_max_ts = max_ts
        except Exception as e:
            print(f"  {i:3}/{len(dbs)} ERROR {slug}: {type(e).__name__}: {e}")

    update_hostname_state(idx_con, HOSTNAME, len(dbs), global_max_ts)
    idx_con.commit()

    # Final counts
    total_rows = idx_con.execute("SELECT COUNT(*) FROM events_index").fetchone()[0]
    fts_rows = idx_con.execute("SELECT COUNT(*) FROM events_index_fts").fetchone()[0]
    idx_con.close()

    total_dur = time.time() - start_t
    print(f"=== ROLLUP SUMMARY ===")
    print(f"  Projects scanned:      {len(dbs)}")
    print(f"  Events inserted:       {total_inserted:,}")
    print(f"  Total events_index:    {total_rows:,}")
    print(f"  Total FTS rows:        {fts_rows:,}")
    print(f"  Hostname:              {HOSTNAME}")
    print(f"  Max event ts:          {global_max_ts}")
    print(f"  Wall:                  {total_dur:.1f}s")

    return 0


if __name__ == "__main__":
    sys.exit(main())

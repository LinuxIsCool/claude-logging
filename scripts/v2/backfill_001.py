#!/usr/bin/env python3
"""task-508 Phase 1.2 — backfill new columns from existing event data.

Populates 3 deterministic new columns on existing events:

  - tool_name        from PreToolUse + PostToolUse + PostToolUseFailure data.tool_name
  - tool_input_hash  sha256(json.dumps(data.tool_input, sort_keys=True))[:16]
  - duration_ms      PostToolUse.ts - paired PreToolUse.ts (by tool_use_id within session)

Columns NOT backfilled (require source data not present in historical events):
  - persona       — depends on claude-matrix agent record join (Phase 1.4 capture-time)
  - agent_id      — only present in newer events with PERSONA_SLUG env
  - tokens_in     — token usage NOT in hook event payload (response field only)
  - tokens_out    — same
  - cost_usd      — derivative; awaits token columns

Idempotent: rows where target column is already non-null are skipped (UPDATE
... WHERE column IS NULL). Re-runnable.

Default mode: --dry-run (counts what WOULD be updated; no writes).
Explicit mode: --apply (actually runs UPDATEs).

Usage:
    uv run python scripts/v2/backfill_001.py            # dry-run all DBs
    uv run python scripts/v2/backfill_001.py --apply    # apply across all DBs
    uv run python scripts/v2/backfill_001.py --apply --limit 5
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

LOGGING_ROOT = Path.home() / ".claude" / "local" / "logging"

TOOL_EVENT_TYPES = ("PreToolUse", "PostToolUse", "PostToolUseFailure")


def discover_dbs() -> list[Path]:
    dbs = []
    for slug_dir in sorted(LOGGING_ROOT.iterdir()):
        if not slug_dir.is_dir() or slug_dir.name == "_index":
            continue
        db = slug_dir / "db" / "logging.db"
        if db.exists() and db.stat().st_size > 0:
            dbs.append(db)
    return dbs


def backfill_db(db_path: Path, dry_run: bool = True) -> dict:
    """Backfill one DB. Returns stats dict."""
    stats = {
        "tool_name_updated": 0,
        "tool_input_hash_updated": 0,
        "duration_ms_updated": 0,
        "skipped_already_set": 0,
        "errors": 0,
    }

    con = sqlite3.connect(db_path, timeout=30.0)
    con.row_factory = sqlite3.Row

    # Phase A: tool_name + tool_input_hash backfill
    cursor = con.execute(
        "SELECT id, type, data FROM events "
        "WHERE type IN (?, ?, ?) AND tool_name IS NULL",
        TOOL_EVENT_TYPES,
    )
    updates_a = []  # list of (tool_name, tool_input_hash, event_id)
    for row in cursor:
        try:
            data = json.loads(row["data"])
            tool_name = data.get("tool_name")
            tool_input = data.get("tool_input")
            if not tool_name:
                continue
            input_hash = None
            if tool_input is not None:
                input_json = json.dumps(tool_input, sort_keys=True, default=str)
                input_hash = hashlib.sha256(input_json.encode()).hexdigest()[:16]
            updates_a.append((tool_name, input_hash, row["id"]))
        except (json.JSONDecodeError, Exception):
            stats["errors"] += 1

    if updates_a and not dry_run:
        con.executemany(
            "UPDATE events SET tool_name = ?, tool_input_hash = ? WHERE id = ?",
            updates_a,
        )
        con.commit()

    stats["tool_name_updated"] = len(updates_a)
    stats["tool_input_hash_updated"] = sum(1 for u in updates_a if u[1] is not None)

    # Phase B: duration_ms backfill — pair PreToolUse → PostToolUse(Failure) by
    # tool_use_id within session
    cursor = con.execute(
        "SELECT id, session_id, type, ts, data FROM events "
        "WHERE type IN (?, ?, ?) AND duration_ms IS NULL "
        "ORDER BY session_id, ts",
        TOOL_EVENT_TYPES,
    )

    # Build pre-by-tool_use_id index per session
    pre_idx: dict[tuple[str, str], tuple[str, str]] = {}  # (session_id, tool_use_id) -> (event_id, ts)
    posts: list[tuple[str, str, str, str]] = []  # (event_id, session_id, ts, tool_use_id)

    for row in cursor:
        try:
            data = json.loads(row["data"])
            tool_use_id = data.get("tool_use_id")
            if not tool_use_id:
                continue
            if row["type"] == "PreToolUse":
                pre_idx[(row["session_id"], tool_use_id)] = (row["id"], row["ts"])
            else:  # PostToolUse / PostToolUseFailure
                posts.append((row["id"], row["session_id"], row["ts"], tool_use_id))
        except json.JSONDecodeError:
            stats["errors"] += 1

    updates_b = []  # (duration_ms, event_id)
    from datetime import datetime
    for post_event_id, session_id, post_ts, tool_use_id in posts:
        pre = pre_idx.get((session_id, tool_use_id))
        if not pre:
            continue
        try:
            pre_dt = datetime.fromisoformat(pre[1].replace("Z", "+00:00"))
            post_dt = datetime.fromisoformat(post_ts.replace("Z", "+00:00"))
            duration_ms = int((post_dt - pre_dt).total_seconds() * 1000)
            if duration_ms < 0:
                continue
            updates_b.append((duration_ms, post_event_id))
        except (ValueError, TypeError):
            stats["errors"] += 1

    if updates_b and not dry_run:
        con.executemany(
            "UPDATE events SET duration_ms = ? WHERE id = ?",
            updates_b,
        )
        con.commit()

    stats["duration_ms_updated"] = len(updates_b)

    con.close()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Actually apply updates (default: dry-run)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N DBs (testing)")
    args = parser.parse_args()

    dbs = discover_dbs()
    print(f"Discovered {len(dbs)} per-project DBs")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print("")

    totals = {
        "tool_name_updated": 0,
        "tool_input_hash_updated": 0,
        "duration_ms_updated": 0,
        "errors": 0,
    }
    start_t = time.time()
    processed = 0

    for i, db in enumerate(dbs, 1):
        slug = db.parent.parent.name
        size_mb = db.stat().st_size / 1024 / 1024
        t0 = time.time()
        try:
            stats = backfill_db(db, dry_run=not args.apply)
        except Exception as e:
            print(f"  {i:3}/{len(dbs)} ✗ {slug:<70} ({size_mb:5.1f}MB) ERROR: {type(e).__name__}: {e}")
            continue
        dur_ms = (time.time() - t0) * 1000

        marker = "✓" if args.apply else "·"
        print(f"  {i:3}/{len(dbs)} {marker} {slug:<60} ({size_mb:5.1f}MB, {dur_ms:6.0f}ms) "
              f"tools={stats['tool_name_updated']} hashes={stats['tool_input_hash_updated']} "
              f"durations={stats['duration_ms_updated']} errs={stats['errors']}")

        for k in totals:
            totals[k] += stats.get(k, 0)
        processed += 1

        if args.limit and processed >= args.limit:
            print(f"  -- stopping at --limit={args.limit}")
            break

    total_dur = time.time() - start_t
    print("")
    print(f"=== SUMMARY ({'APPLIED' if args.apply else 'DRY-RUN'}) ===")
    print(f"  DBs processed:           {processed}")
    print(f"  tool_name backfilled:    {totals['tool_name_updated']:,}")
    print(f"  tool_input_hash filled:  {totals['tool_input_hash_updated']:,}")
    print(f"  duration_ms backfilled:  {totals['duration_ms_updated']:,}")
    print(f"  Errors:                  {totals['errors']:,}")
    print(f"  Total wall:              {total_dur:.1f}s")

    return 0


if __name__ == "__main__":
    sys.exit(main())

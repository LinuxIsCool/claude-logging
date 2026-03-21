#!/usr/bin/env python3
"""
Backfill JSONL sessions into SQLite logging.db.

Syncs all JSONL session files that are not yet tracked in sync_state.
Run this to recover from sync gaps (e.g., the March 15-20 outage).

Usage:
    uv run scripts/sync_backfill.py [--dry-run] [--project-path /home/shawn]
"""

import argparse
import sys
import time
from pathlib import Path

# Add parent dir so we can import lib/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.storage import StorageManager


def encode_project_path(project_dir: str) -> str:
    return project_dir.replace("/", "-")


def main():
    parser = argparse.ArgumentParser(description="Backfill JSONL → SQLite sync")
    parser.add_argument(
        "--project-path", default="/home/shawn",
        help="Project directory (default: /home/shawn)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be synced without syncing"
    )
    args = parser.parse_args()

    encoded = encode_project_path(args.project_path)
    base_path = Path.home() / ".claude" / "local" / "logging" / encoded

    sessions_dir = base_path / "sessions"
    if not sessions_dir.exists():
        print(f"No sessions directory at {sessions_dir}")
        sys.exit(1)

    # Count JSONL files
    jsonl_files = list(sessions_dir.glob("*.jsonl"))
    print(f"JSONL session files: {len(jsonl_files)}")

    sm = StorageManager(base_path)

    # Get already-synced sessions
    cursor = sm.sqlite.conn.execute("SELECT session_id FROM sync_state")
    synced_ids = {row[0] for row in cursor}
    print(f"Already synced: {len(synced_ids)}")

    # Find unsynced
    unsynced = []
    for f in jsonl_files:
        session_id = f.stem
        if session_id not in synced_ids:
            unsynced.append(session_id)
        else:
            # Check if partially synced (file grew since last sync)
            current_pos = f.stat().st_size
            last_pos = sm.sqlite.get_sync_position(session_id)
            if current_pos > last_pos:
                unsynced.append(session_id)

    print(f"Need sync: {len(unsynced)}")

    if args.dry_run:
        for sid in unsynced[:20]:
            print(f"  would sync: {sid}")
        if len(unsynced) > 20:
            print(f"  ... and {len(unsynced) - 20} more")
        sm.close()
        return

    # Sync in batches
    total_events = 0
    errors = 0
    start = time.time()

    for i, session_id in enumerate(unsynced):
        try:
            events = sm.sync_session(session_id)
            total_events += events
            if (i + 1) % 50 == 0:
                elapsed = time.time() - start
                rate = (i + 1) / elapsed
                print(f"  [{i+1}/{len(unsynced)}] {total_events} events, {rate:.1f} sessions/sec")
        except Exception as e:
            errors += 1
            print(f"  ERROR syncing {session_id}: {e}")

    elapsed = time.time() - start

    # Final counts
    cursor = sm.sqlite.conn.execute("SELECT COUNT(*) FROM sessions")
    final_sessions = cursor.fetchone()[0]
    cursor = sm.sqlite.conn.execute("SELECT COUNT(*) FROM events")
    final_events = cursor.fetchone()[0]

    print(f"\nBackfill complete in {elapsed:.1f}s")
    print(f"  Sessions synced: {len(unsynced) - errors}")
    print(f"  Events added: {total_events}")
    print(f"  Errors: {errors}")
    print(f"  DB totals: {final_sessions} sessions, {final_events} events")

    # Write heartbeat on success
    health_dir = Path.home() / ".claude" / "local" / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    hb = health_dir / "logging-heartbeat"
    hb.write_text(f"{datetime.now(timezone.utc).isoformat()}\n")
    print(f"  Heartbeat written: {hb}")

    sm.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Backfill completed native Codex rollouts into the unified logging archive."""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hooks.log_event import get_storage_path  # noqa: E402
from lib.codex_backfill import events_from_rollout  # noqa: E402
from lib.storage import StorageManager  # noqa: E402

CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
INDEX_DB = Path.home() / ".claude" / "local" / "logging" / "_index" / "index.db"


def known_sessions() -> set[str]:
    if not INDEX_DB.exists():
        return set()
    con = sqlite3.connect(f"file:{INDEX_DB}?mode=ro", uri=True)
    try:
        return {row[0] for row in con.execute("SELECT DISTINCT session_id FROM events_index")}
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-active", action="store_true")
    args = parser.parse_args()
    known = known_sessions()
    report = {"discovered": 0, "imported": 0, "events": 0, "known": 0, "active": 0, "empty": 0}
    for path in sorted(CODEX_SESSIONS.rglob("*.jsonl")):
        report["discovered"] += 1
        if not args.include_active and time.time() - path.stat().st_mtime < 300:
            report["active"] += 1
            continue
        rollout = events_from_rollout(path)
        if rollout.session_id in known:
            report["known"] += 1
            continue
        if not rollout.events:
            report["empty"] += 1
            continue
        report["imported"] += 1
        report["events"] += len(rollout.events)
        if args.dry_run:
            continue
        store = StorageManager(get_storage_path(rollout.cwd or None))
        try:
            archive = store.jsonl.get_session_path(rollout.session_id)
            archived_ids = set()
            if archive.exists():
                archived_ids = {
                    row.get("id") for row in store.jsonl.read_session(rollout.session_id)
                    if isinstance(row, dict)
                }
            for event in rollout.events:
                if event.id not in archived_ids:
                    store.jsonl.append_event(event)
            store.sync_session(rollout.session_id)
        finally:
            store.close()
        known.add(rollout.session_id)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

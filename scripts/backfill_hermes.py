#!/usr/bin/env python3
"""Backfill Hermes' native SQLite sessions into the unified archive."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hooks.log_event import get_storage_path  # noqa: E402
from lib.hermes_backfill import sessions_from_db  # noqa: E402
from lib.storage import StorageManager  # noqa: E402

HERMES_DB = Path.home() / ".hermes" / "state.db"
INDEX_DB = Path.home() / ".claude" / "local" / "logging" / "_index" / "index.db"


def known_sessions() -> set[str]:
    if not INDEX_DB.exists():
        return set()
    con = sqlite3.connect(f"file:{INDEX_DB}?mode=ro", uri=True)
    try:
        return {row[0] for row in con.execute("SELECT DISTINCT session_id FROM events_index WHERE runtime='hermes'")}
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    known = known_sessions()
    report = {"discovered": 0, "imported": 0, "known": 0, "events": 0}
    for session in sessions_from_db(HERMES_DB) if HERMES_DB.exists() else []:
        report["discovered"] += 1
        if session.session_id in known:
            report["known"] += 1
            continue
        report["imported"] += 1
        report["events"] += len(session.events)
        if args.dry_run:
            continue
        store = StorageManager(get_storage_path(session.cwd or None))
        try:
            for event in session.events:
                store.jsonl.append_event(event)
            store.sync_session(session.session_id)
        finally:
            store.close()
        known.add(session.session_id)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

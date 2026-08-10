#!/usr/bin/env python3
"""Backfill Pi-family session trees into the unified logging archive."""

from __future__ import annotations

import argparse, sqlite3, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hooks.log_event import get_storage_path  # noqa: E402
from lib.pi_backfill import events_from_session  # noqa: E402
from lib.storage import StorageManager  # noqa: E402

PI_SESSIONS = Path.home() / ".pi" / "agent" / "sessions"
SESSION_ROOTS = {
    "pi": PI_SESSIONS,
    "prime-agent": Path.home() / ".prime" / "agent" / "sessions",
    "omp": Path.home() / ".omp" / "agent" / "sessions",
}
INDEX_DB = Path.home() / ".claude" / "local" / "logging" / "_index" / "index.db"


def known_sessions(runtime: str):
    if not INDEX_DB.exists(): return set()
    con = sqlite3.connect(f"file:{INDEX_DB}?mode=ro", uri=True)
    try: return {r[0] for r in con.execute("SELECT DISTINCT session_id FROM events_index WHERE runtime=?", (runtime,))}
    finally: con.close()


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--runtime", choices=SESSION_ROOTS, default="pi"); parser.add_argument("--dry-run", action="store_true"); parser.add_argument("--include-active", action="store_true"); args = parser.parse_args()
    session_root = SESSION_ROOTS[args.runtime]
    known = known_sessions(args.runtime); report = {"runtime":args.runtime,"discovered":0,"imported":0,"enriched":0,"events":0,"known":0,"active":0}
    for path in sorted(session_root.rglob("*.jsonl")) if session_root.exists() else []:
        report["discovered"] += 1
        if not args.include_active and time.time() - path.stat().st_mtime < 300: report["active"] += 1; continue
        session = events_from_session(path, runtime=args.runtime)
        is_known = session.session_id in known
        if is_known: report["known"] += 1
        else: report["imported"] += 1
        if args.dry_run:
            report["events"] += len(session.events) if not is_known else 0
            continue
        store = StorageManager(get_storage_path(session.cwd or None))
        try:
            archived = {r.get("id") for r in store.jsonl.read_session(session.session_id)} if store.jsonl.get_session_path(session.session_id).exists() else set()
            events = session.events
            if is_known:
                # Live capture owns conversational/tool events. Archive repair
                # adds only Pi tree/session metadata that startup events cannot
                # always observe (for example CLI --name/model selection).
                enrichment_types = {"SessionInfo", "ModelChange", "ThinkingLevelChange", "BranchSummary", "PostCompact", "CustomMessage"}
                existing_types = {row[0] for row in store.sqlite.conn.execute("SELECT type FROM events WHERE session_id=?", (session.session_id,))}
                events = [event for event in events if event.type in enrichment_types and event.type not in existing_types]
                report["enriched"] += len(events)
            report["events"] += len(events)
            for event in events:
                if event.id not in archived: store.jsonl.append_event(event)
            store.sync_session(session.session_id)
        finally: store.close()
        known.add(session.session_id)
    print(report); return 0


if __name__ == "__main__": raise SystemExit(main())

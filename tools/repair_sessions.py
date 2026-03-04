#!/usr/bin/env python3
"""
Repair tool: Retroactively add missing AssistantResponse events.

Scans sessions for Stop events that don't have a following AssistantResponse,
then extracts the response from the original transcript file and inserts it.

Usage:
    python repair_sessions.py [--dry-run] [session_id]

    --dry-run: Show what would be fixed without making changes
    session_id: Optional - repair specific session, otherwise repair all
"""

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timezone


def get_response(transcript_path: str) -> str:
    """Extract last assistant response from Claude's transcript."""
    try:
        for line in reversed(Path(transcript_path).read_text().strip().split("\n")):
            if line.strip():
                entry = json.loads(line)
                if entry.get("type") == "assistant":
                    for block in entry.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            return block.get("text", "")
    except Exception:
        pass
    return ""


def analyze_session(session_path: Path) -> list:
    """Find Stop events missing AssistantResponse in a session."""
    events = []
    with open(session_path) as f:
        for line in f:
            if line.strip():
                events.append(json.loads(line))

    missing = []
    i = 0
    while i < len(events):
        event = events[i]
        if event.get("type") == "Stop":
            transcript_path = event.get("data", {}).get("transcript_path")

            # Check if next event is AssistantResponse
            has_response = (
                i + 1 < len(events) and
                events[i + 1].get("type") == "AssistantResponse"
            )

            if not has_response and transcript_path:
                missing.append({
                    "index": i,
                    "stop_event": event,
                    "transcript_path": transcript_path,
                    "transcript_exists": Path(transcript_path).exists()
                })
        i += 1

    return missing


def repair_session(session_path: Path, dry_run: bool = True) -> dict:
    """Repair a session by adding missing AssistantResponse events."""
    missing = analyze_session(session_path)

    if not missing:
        return {"session": session_path.stem, "missing": 0, "repaired": 0, "failed": 0, "skipped": 0}

    # Read all events
    events = []
    with open(session_path) as f:
        for line in f:
            if line.strip():
                events.append(json.loads(line))

    repaired = 0
    failed = 0
    skipped = 0
    last_response = None  # Track to skip duplicates

    # Process in reverse order to maintain indices
    for m in reversed(missing):
        idx = m["index"]
        stop_event = m["stop_event"]
        transcript_path = m["transcript_path"]

        if not m["transcript_exists"]:
            print(f"  ✗ Transcript missing: {transcript_path[:60]}...")
            failed += 1
            continue

        response = get_response(transcript_path)
        if not response:
            print(f"  ✗ No response found in: {transcript_path[:60]}...")
            failed += 1
            continue

        # Skip if this is the same response we just inserted (duplicate detection)
        if response == last_response:
            preview = response[:40].replace('\n', ' ')
            print(f"  ⊘ Skipped duplicate: {preview}...")
            skipped += 1
            continue

        last_response = response

        # Create AssistantResponse event
        assistant_event = {
            "id": f"evt_repair_{idx:04d}",
            "type": "AssistantResponse",
            "ts": stop_event["ts"],  # Same timestamp as Stop
            "session_id": stop_event["session_id"],
            "agent_session_num": stop_event.get("agent_session_num", 0),
            "data": {"response": response},
            "content": response,
        }

        # Insert after Stop event
        events.insert(idx + 1, assistant_event)
        repaired += 1

        preview = response[:60].replace('\n', ' ')
        print(f"  ✓ Added response ({len(response)} chars): {preview}...")

    if not dry_run and repaired > 0:
        # Write repaired events back
        with open(session_path, "w") as f:
            for event in events:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(f"  → Saved {len(events)} events to {session_path.name}")

    return {
        "session": session_path.stem[:8],
        "missing": len(missing),
        "repaired": repaired,
        "failed": failed,
        "skipped": skipped
    }


def main():
    parser = argparse.ArgumentParser(description="Repair sessions with missing AssistantResponse events")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fixed without making changes")
    parser.add_argument("session_id", nargs="?", help="Specific session ID to repair (default: all)")
    parser.add_argument("--storage", default=None, help="Storage path (default: auto-detect)")
    args = parser.parse_args()

    # Find storage path
    if args.storage:
        storage_path = Path(args.storage)
    else:
        # Try common locations
        candidates = [
            Path.cwd() / ".claude/local/logging",
            Path.home() / ".claude/local/logging",
        ]
        storage_path = next((p for p in candidates if p.exists()), None)

        if not storage_path:
            print("Error: Could not find logging storage. Use --storage to specify.")
            sys.exit(1)

    sessions_dir = storage_path / "sessions"
    if not sessions_dir.exists():
        print(f"Error: Sessions directory not found: {sessions_dir}")
        sys.exit(1)

    # Find sessions to repair
    if args.session_id:
        session_files = list(sessions_dir.glob(f"{args.session_id}*.jsonl"))
    else:
        session_files = list(sessions_dir.glob("*.jsonl"))

    if not session_files:
        print("No sessions found to repair.")
        return

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Scanning {len(session_files)} sessions...\n")

    total_missing = 0
    total_repaired = 0
    total_failed = 0
    total_skipped = 0

    for session_path in sorted(session_files):
        missing = analyze_session(session_path)
        if missing:
            print(f"\n{session_path.stem[:8]}... ({len(missing)} missing)")
            result = repair_session(session_path, dry_run=args.dry_run)
            total_missing += result["missing"]
            total_repaired += result["repaired"]
            total_failed += result["failed"]
            total_skipped += result.get("skipped", 0)

    print(f"\n{'='*50}")
    print(f"Summary: {total_missing} missing, {total_repaired} repaired, {total_skipped} skipped (duplicates), {total_failed} failed")

    if args.dry_run and total_repaired > 0:
        print(f"\nRun without --dry-run to apply repairs.")


if __name__ == "__main__":
    main()

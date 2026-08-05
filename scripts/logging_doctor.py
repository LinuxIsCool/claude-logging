#!/usr/bin/env python3
"""Completeness watchdog for claude-logging.

Compares Claude Code's transcripts (~/.claude/projects/) against what the
plugin actually stored, and optionally repairs the difference. See lib/doctor.py
for why the reference point has to be the transcripts and not the plugin's own
archive.

Usage:
    uv run scripts/logging_doctor.py                # report only
    uv run scripts/logging_doctor.py --repair       # report and backfill
    uv run scripts/logging_doctor.py --json         # machine-readable

Exit codes:
    0  complete (nothing recoverable is missing)
    1  gaps remain
    2  the doctor itself failed to run
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.doctor import DOCTOR_STALE_HOURS, heartbeat_age_hours, run_doctor


def main() -> int:
    parser = argparse.ArgumentParser(description="claude-logging completeness watchdog")
    parser.add_argument("--repair", action="store_true", help="backfill missing sessions")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--projects-root", default=str(Path.home() / ".claude" / "projects")
    )
    parser.add_argument(
        "--logging-root", default=str(Path.home() / ".claude" / "local" / "logging")
    )
    args = parser.parse_args()

    logging_root = Path(args.logging_root)

    try:
        report = run_doctor(Path(args.projects_root), logging_root, repair=args.repair)
    except Exception as exc:  # noqa: BLE001 - a watchdog must report its own failure
        print(f"doctor failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"healthy": report.healthy, **report.__dict__}, indent=2, default=str))
        return 0 if report.healthy else 1

    degraded = [p for p in report.projects if p["recoverable_missing"]]
    repaired = [p for p in report.projects if p["repaired"]]

    print(f"projects checked : {len(report.projects)}")
    if repaired:
        print(f"repaired         : {report.total_repaired} sessions, {report.total_events_added} events")
        for p in repaired:
            print(f"    + {p['repaired']:4d} sessions  {p['slug']}")

    if degraded:
        print(f"GAPS REMAIN      : {report.total_missing} sessions uncaptured")
        for p in degraded:
            print(f"    ! {p['recoverable_missing']:4d} missing   {p['slug']}")
        if not args.repair:
            print("\nrun with --repair to recover them")
    else:
        print("completeness     : OK (every recoverable transcript is stored)")

    age = heartbeat_age_hours(logging_root)
    if age > DOCTOR_STALE_HOURS:
        print(f"note: previous run was {age:.0f}h ago (threshold {DOCTOR_STALE_HOURS}h)")

    return 0 if report.healthy else 1


if __name__ == "__main__":
    sys.exit(main())

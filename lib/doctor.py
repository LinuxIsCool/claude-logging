"""doctor — the completeness watchdog, and the thing this plugin never had.

Between 2026-06-30 and 2026-07-16 claude-logging captured nothing at all, and
nobody noticed for 15 days. The reason is worth stating precisely, because it
determines what this module is allowed to check:

    Every completeness check the plugin had compared SQLite against the
    plugin's OWN archive. When the hook never fires, no archive file is
    written either. Both sides are empty, both sides agree, and the check
    reports success while the plugin is stone dead.

A watchdog is only as good as its reference point. This one compares against
Claude Code's transcripts under ~/.claude/projects/, which are written by the
harness and therefore survive any failure of ours.

What it alarms on
-----------------
1. Recoverable sessions missing from the store. A transcript exists, it
   contains real conversation, and the store has no row for it. Unambiguous:
   capture was dead for that session.
2. Its own staleness. A watchdog that stops running looks exactly like a
   healthy system, so it stamps `ran_at` on every run and that timestamp is
   itself checked.

What it deliberately does NOT alarm on
--------------------------------------
Event-count shortfall within a captured session. The live Stop hook writes at
most one AssistantResponse per turn (only the final message; intermediate
tool-call turns are never stored) and writes none at all for interrupted turns.
Measured on the real store, roughly two thirds of turns yield assistant text.
So a store legitimately holds a small fraction of the transcript's lines, and
an event-count floor would fire every day on healthy data. An alarm that fires
every day is an alarm nobody reads, which is the failure mode that let a
15-day outage pass unnoticed in the first place. Session-level presence is the
signal that is actually unambiguous; that is the one we use.

Repair, not just report
-----------------------
Reporting alone would have left the 37-session hole sitting there. With
--repair the doctor backfills what it finds, so the invariant converges on its
own instead of accruing a backlog that needs a human to notice it.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from lib.storage import StorageManager
from lib.transcript_backfill import (
    backfill_project,
    events_from_transcript,
    missing_sessions,
)

# A daily timer with a day of slack: two consecutive misses is a real problem,
# one is a laptop that was asleep.
DOCTOR_STALE_HOURS = 48

HEALTH_DIRNAME = "_health"
STATUS_FILENAME = "doctor.json"


@dataclass
class ProjectHealth:
    slug: str
    transcripts: int = 0
    stored_sessions: int = 0
    recoverable_missing: int = 0
    empty_transcripts: int = 0
    repaired: int = 0
    events_added: int = 0

    @property
    def healthy(self) -> bool:
        return self.recoverable_missing == 0


@dataclass
class DoctorReport:
    ran_at: float = 0.0
    total_missing: int = 0
    total_repaired: int = 0
    total_events_added: int = 0
    projects: list[dict] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.total_missing == 0


def diagnose(store: StorageManager, transcript_dir: Path, slug: str) -> ProjectHealth:
    """Compare one project's transcripts against its store."""
    health = ProjectHealth(slug=slug)
    health.transcripts = len(list(transcript_dir.glob("*.jsonl")))
    health.stored_sessions = store.sqlite.conn.execute(
        "SELECT COUNT(*) FROM sessions"
    ).fetchone()[0]

    for session_id in missing_sessions(store, transcript_dir):
        # A stub with no conversation (orphaned ai-title/agent-name files) has
        # nothing to recover. Counting it would leave a permanent phantom gap
        # that never closes, and a gap that never closes trains you to ignore
        # the alarm.
        if events_from_transcript(transcript_dir / f"{session_id}.jsonl", session_id):
            health.recoverable_missing += 1
        else:
            health.empty_transcripts += 1

    return health


def heartbeat_age_hours(logging_root: Path) -> float:
    """Hours since the doctor last completed. inf if it has never run."""
    status = logging_root / HEALTH_DIRNAME / STATUS_FILENAME
    try:
        ran_at = json.loads(status.read_text()).get("ran_at")
        return (time.time() - float(ran_at)) / 3600
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return float("inf")


def run_doctor(
    projects_root: Path, logging_root: Path, repair: bool = False
) -> DoctorReport:
    """Check every project that has transcripts, optionally repairing gaps."""
    report = DoctorReport(ran_at=time.time())

    if projects_root.exists():
        for transcript_dir in sorted(p for p in projects_root.iterdir() if p.is_dir()):
            if not any(transcript_dir.glob("*.jsonl")):
                continue

            slug = transcript_dir.name
            # StorageManager creates the store on construction, which is what
            # recovers a project that never captured anything at all.
            store = StorageManager(logging_root / slug)
            try:
                if repair:
                    result = backfill_project(store, transcript_dir)
                    health = diagnose(store, transcript_dir, slug)
                    health.repaired = result.sessions_added
                    health.events_added = result.events_added
                else:
                    health = diagnose(store, transcript_dir, slug)
            finally:
                store.close()

            report.total_missing += health.recoverable_missing
            report.total_repaired += health.repaired
            report.total_events_added += health.events_added
            report.projects.append(asdict(health))

    _write_status(logging_root, report)
    return report


def _write_status(logging_root: Path, report: DoctorReport) -> None:
    health_dir = logging_root / HEALTH_DIRNAME
    health_dir.mkdir(parents=True, exist_ok=True)
    payload = asdict(report)
    payload["healthy"] = report.healthy
    # Written atomically: a torn status file would read as "never ran" and
    # produce a false watchdog-is-dead alarm.
    tmp = health_dir / (STATUS_FILENAME + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(health_dir / STATUS_FILENAME)

"""Tests for lib/doctor.py — the completeness watchdog.

The plugin died silently for 15 days. Every check it had compared SQLite to the
plugin's own archive, so when the hook never fired, both sides were empty and
agreed. These tests pin the two checks that actually catch that, and pin the
one check we deliberately refuse to make.
"""

import json
import sys
import time
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from lib.doctor import DOCTOR_STALE_HOURS, ProjectHealth, diagnose, run_doctor
from lib.storage import StorageManager


def _line(**kw):
    return json.dumps(kw)


def make_transcript(d: Path, sid: str, prompt="hello world"):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text(
        _line(
            type="user",
            uuid=f"u-{sid}",
            timestamp="2026-07-01T10:00:00.000Z",
            cwd="/home/shawn",
            message={"role": "user", "content": prompt},
        )
        + "\n"
    )


def make_empty_transcript(d: Path, sid: str):
    """An orphaned stub: real file, no conversation. Nothing to recover."""
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text(
        _line(type="ai-title", uuid="t-1") + "\n" + _line(type="agent-name", uuid="n-1") + "\n"
    )


class TestDiagnose:
    def test_healthy_project_reports_no_gap(self, tmp_path):
        tdir = tmp_path / "projects" / "proj"
        make_transcript(tdir, "s1")
        store = StorageManager(tmp_path / "store")
        from lib.transcript_backfill import backfill_project

        backfill_project(store, tdir)

        health = diagnose(store, tdir, "proj")
        assert health.recoverable_missing == 0
        assert health.healthy

    def test_uncaptured_session_is_a_gap(self, tmp_path):
        tdir = tmp_path / "projects" / "proj"
        make_transcript(tdir, "s1")
        store = StorageManager(tmp_path / "store")

        health = diagnose(store, tdir, "proj")
        assert health.recoverable_missing == 1
        assert not health.healthy

    def test_empty_transcript_is_not_an_alarm(self, tmp_path):
        """An orphaned stub has nothing to recover. Counting it would make the
        watchdog cry wolf forever, which is how watchdogs get ignored."""
        tdir = tmp_path / "projects" / "proj"
        make_empty_transcript(tdir, "orphan.orphaned-123")
        store = StorageManager(tmp_path / "store")

        health = diagnose(store, tdir, "proj")
        assert health.empty_transcripts == 1
        assert health.recoverable_missing == 0
        assert health.healthy

    def test_does_not_alarm_on_partial_assistant_capture(self, tmp_path):
        """Deliberate non-check. The live Stop hook writes at most one
        AssistantResponse per turn and none for interrupted turns, so a store
        legitimately holds far fewer events than the transcript has lines. An
        event-count floor would fire every single day."""
        tdir = tmp_path / "projects" / "proj"
        sid = "s1"
        tdir.mkdir(parents=True)
        (tdir / f"{sid}.jsonl").write_text(
            "\n".join(
                _line(
                    type="assistant",
                    uuid=f"a-{i}",
                    timestamp="2026-07-01T10:00:00.000Z",
                    message={"role": "assistant", "content": [{"type": "text", "text": f"t{i}"}]},
                )
                for i in range(50)
            )
            + "\n"
        )
        store = StorageManager(tmp_path / "store")
        store.sqlite.conn.execute(
            "INSERT INTO sessions (id, started_at) VALUES (?, ?)", (sid, "2026-07-01T10:00:00Z")
        )
        store.sqlite.conn.commit()

        health = diagnose(store, tdir, "proj")
        assert health.recoverable_missing == 0
        assert health.healthy, "partial event capture must not trip the alarm"


class TestRunDoctor:
    def test_repair_closes_the_gap(self, tmp_path):
        proj = tmp_path / "projects"
        make_transcript(proj / "alpha", "s1")
        make_transcript(proj / "beta", "s2")

        first = run_doctor(proj, tmp_path / "logging", repair=True)
        assert first.total_repaired == 2

        second = run_doctor(proj, tmp_path / "logging", repair=True)
        assert second.total_repaired == 0
        assert second.healthy

    def test_readonly_run_does_not_repair(self, tmp_path):
        proj = tmp_path / "projects"
        make_transcript(proj / "alpha", "s1")

        report = run_doctor(proj, tmp_path / "logging", repair=False)
        assert report.total_repaired == 0
        assert not report.healthy
        assert report.total_missing == 1

    def test_writes_status_file_with_heartbeat(self, tmp_path):
        proj = tmp_path / "projects"
        make_transcript(proj / "alpha", "s1")
        logging_root = tmp_path / "logging"

        run_doctor(proj, logging_root, repair=True)
        status = logging_root / "_health" / "doctor.json"
        assert status.exists()

        payload = json.loads(status.read_text())
        assert "ran_at" in payload, "no heartbeat means a dead watchdog looks like a healthy one"
        assert payload["healthy"] is True
        assert payload["projects"]

    def test_creates_store_for_project_that_never_had_one(self, tmp_path):
        """3 real projects have transcripts and no DB at all. Those are total
        capture failures and must still be recoverable."""
        proj = tmp_path / "projects"
        make_transcript(proj / "never-captured", "s1")

        report = run_doctor(proj, tmp_path / "logging", repair=True)
        assert report.total_repaired == 1
        assert (tmp_path / "logging" / "never-captured" / "db" / "logging.db").exists()


class TestStaleness:
    def test_stale_heartbeat_is_detectable(self, tmp_path):
        """The watchdog must be able to report its own death."""
        from lib.doctor import heartbeat_age_hours

        logging_root = tmp_path / "logging"
        health_dir = logging_root / "_health"
        health_dir.mkdir(parents=True)
        old = time.time() - (DOCTOR_STALE_HOURS + 5) * 3600
        (health_dir / "doctor.json").write_text(json.dumps({"ran_at": old}))

        assert heartbeat_age_hours(logging_root) > DOCTOR_STALE_HOURS

    def test_missing_heartbeat_reads_as_infinitely_stale(self, tmp_path):
        from lib.doctor import heartbeat_age_hours

        assert heartbeat_age_hours(tmp_path / "nope") == float("inf")

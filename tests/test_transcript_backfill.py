"""Tests for lib/transcript_backfill.py — recovering sessions that capture missed.

The plugin's normal path is transcript -> hook -> plugin archive -> SQLite. When
the hook is dead (wrong manifest path, crashed uv, disabled plugin) NOTHING is
written and there is no archive file to re-sync from. The only surviving record
is Claude Code's own transcript under ~/.claude/projects/<slug>/<session>.jsonl.

This module reconstructs the event stream from that transcript. These tests pin
the two properties that make it safe to run against a live 89MB store:
idempotency (stable IDs -> re-running never duplicates) and non-interference
(it must never touch a session the live hook already owns).
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from lib.storage import StorageManager
from lib.transcript_backfill import (
    backfill_project,
    events_from_transcript,
    missing_sessions,
)


def _line(**kw):
    return json.dumps(kw)


def write_transcript(path: Path, session_id: str) -> Path:
    """A transcript with one user turn and one assistant turn using a tool."""
    lines = [
        _line(
            type="user",
            uuid="u-1",
            timestamp="2026-07-01T10:00:00.000Z",
            sessionId=session_id,
            cwd="/home/shawn",
            message={"role": "user", "content": "fix the parser"},
        ),
        _line(
            type="assistant",
            uuid="a-1",
            timestamp="2026-07-01T10:00:05.000Z",
            sessionId=session_id,
            message={
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [
                    {"type": "text", "text": "Let me look at the parser."},
                    {
                        "type": "tool_use",
                        "id": "tu-1",
                        "name": "Read",
                        "input": {"file_path": "/tmp/parser.py"},
                    },
                ],
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        ),
        _line(
            type="user",
            uuid="u-2",
            timestamp="2026-07-01T10:00:06.000Z",
            sessionId=session_id,
            message={
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu-1", "content": "def parse(): pass"}
                ],
            },
        ),
        # Noise the importer must ignore: these are UI/bookkeeping lines, not events.
        _line(type="file-history-snapshot", uuid="f-1", timestamp="2026-07-01T10:00:07.000Z"),
        _line(type="mode", uuid="m-1", timestamp="2026-07-01T10:00:08.000Z"),
        _line(
            type="assistant",
            uuid="a-2",
            timestamp="2026-07-01T10:00:09.000Z",
            sessionId=session_id,
            message={
                "role": "assistant",
                "content": [{"type": "text", "text": "Fixed it."}],
            },
        ),
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def transcript(tmp_path):
    d = tmp_path / "projects" / "-home-shawn"
    d.mkdir(parents=True)
    sid = "11111111-2222-3333-4444-555555555555"
    write_transcript(d / f"{sid}.jsonl", sid)
    return d, sid


@pytest.fixture
def store(tmp_path):
    return StorageManager(tmp_path / "store")


class TestEventsFromTranscript:
    def test_extracts_each_event_kind(self, transcript):
        d, sid = transcript
        events = events_from_transcript(d / f"{sid}.jsonl", sid)
        kinds = [e.type for e in events]
        assert kinds.count("UserPromptSubmit") == 1
        assert kinds.count("AssistantResponse") == 2
        assert kinds.count("PreToolUse") == 1
        assert kinds.count("PostToolUse") == 1

    def test_ignores_non_event_lines(self, transcript):
        """file-history-snapshot / mode lines are UI bookkeeping, not events."""
        d, sid = transcript
        events = events_from_transcript(d / f"{sid}.jsonl", sid)
        assert len(events) == 5

    def test_tool_name_is_captured(self, transcript):
        d, sid = transcript
        events = events_from_transcript(d / f"{sid}.jsonl", sid)
        pre = next(e for e in events if e.type == "PreToolUse")
        assert pre.tool_name == "Read"

    def test_content_is_searchable_text(self, transcript):
        d, sid = transcript
        events = events_from_transcript(d / f"{sid}.jsonl", sid)
        prompt = next(e for e in events if e.type == "UserPromptSubmit")
        assert prompt.content == "fix the parser"
        texts = [e.content for e in events if e.type == "AssistantResponse"]
        assert "Fixed it." in texts

    def test_every_event_is_provenance_tagged(self, transcript):
        """A backfilled row must be distinguishable from a live-captured one."""
        d, sid = transcript
        events = events_from_transcript(d / f"{sid}.jsonl", sid)
        assert all(e.data.get("_source") == "transcript-backfill" for e in events)

    def test_ids_are_stable_across_runs(self, transcript):
        """Stable IDs are what make re-running the backfill safe."""
        d, sid = transcript
        a = [e.id for e in events_from_transcript(d / f"{sid}.jsonl", sid)]
        b = [e.id for e in events_from_transcript(d / f"{sid}.jsonl", sid)]
        assert a == b
        assert len(set(a)) == len(a), "ids must be unique within a session"

    def test_torn_final_line_does_not_abort_the_import(self, tmp_path):
        """A session killed mid-write leaves a partial JSON line. The events
        before it are real and must still be recovered."""
        sid = "torn-session"
        p = tmp_path / f"{sid}.jsonl"
        good = _line(
            type="user",
            uuid="u-1",
            timestamp="2026-07-01T10:00:00.000Z",
            message={"role": "user", "content": "hello"},
        )
        p.write_text(good + "\n" + '{"type":"assistant","uuid":"a-1","mess')
        events = events_from_transcript(p, sid)
        assert len(events) == 1
        assert events[0].content == "hello"


class TestMissingSessions:
    def test_finds_transcripts_absent_from_store(self, transcript, store):
        d, sid = transcript
        assert missing_sessions(store, d) == [sid]

    def test_returns_nothing_once_present(self, transcript, store):
        d, sid = transcript
        backfill_project(store, d)
        assert missing_sessions(store, d) == []


class TestBackfillProject:
    def test_inserts_session_and_events(self, transcript, store):
        d, sid = transcript
        report = backfill_project(store, d)
        assert report.sessions_added == 1
        assert report.events_added == 5

        con = store.sqlite.conn
        assert con.execute("SELECT COUNT(*) FROM sessions WHERE id=?", (sid,)).fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM events WHERE session_id=?", (sid,)).fetchone()[0] == 5

    def test_is_idempotent(self, transcript, store):
        """Re-running must not duplicate rows. This is the property the old
        INSERT OR REPLACE FTS bug violated, so it is asserted on FTS too."""
        d, sid = transcript
        backfill_project(store, d)
        second = backfill_project(store, d)

        con = store.sqlite.conn
        assert second.sessions_added == 0
        assert con.execute("SELECT COUNT(*) FROM events WHERE session_id=?", (sid,)).fetchone()[0] == 5
        events_n = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        fts_n = con.execute("SELECT COUNT(*) FROM events_fts").fetchone()[0]
        assert events_n == fts_n, "FTS index drifted from events"

    def test_backfilled_content_is_searchable(self, transcript, store):
        d, _ = transcript
        backfill_project(store, d)
        assert store.search("parser"), "backfilled events must be full-text searchable"

    def test_does_not_touch_sessions_the_live_hook_owns(self, transcript, store):
        """The live path owns any session with a plugin archive file. Backfill
        must skip those so it can never race or clobber real capture."""
        d, sid = transcript
        archive = store.jsonl.get_session_path(sid)
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text("")

        report = backfill_project(store, d)
        assert report.sessions_added == 0
        assert report.skipped_live == 1

    def test_reported_count_equals_rows_written(self, tmp_path, store):
        """Claude Code sometimes writes a transcript line twice. The report must
        count rows actually inserted, not events generated, or the number cannot
        be reconciled against the database."""
        tdir = tmp_path / "projects"
        tdir.mkdir()
        sid = "dup-session"
        line = _line(
            type="user",
            uuid="u-dup",
            timestamp="2026-07-01T10:00:00.000Z",
            message={"role": "user", "content": "duplicated line"},
        )
        (tdir / f"{sid}.jsonl").write_text(line + "\n" + line + "\n")

        report = backfill_project(store, tdir)
        actual = store.sqlite.conn.execute(
            "SELECT COUNT(*) FROM events WHERE session_id=?", (sid,)
        ).fetchone()[0]
        assert report.events_added == actual == 1

    def test_dry_run_writes_nothing(self, transcript, store):
        d, sid = transcript
        report = backfill_project(store, d, dry_run=True)
        assert report.sessions_added == 1
        con = store.sqlite.conn
        assert con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0

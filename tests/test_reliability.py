#!/usr/bin/env python3
"""
Regression tests for reliability fixes (Phase 2 of publication roadmap).

Tests:
- Event schema evolution (unknown fields don't crash sync)
- Cross-platform fcntl shim (no ImportError on any platform)
- Agent session number counting (JSON parsing, not string matching)
- ALLOWED_IMAGE_TYPES is a module-level constant
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest"]
# ///

import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


# ── Event schema evolution ──────────────────────────────────────────


class TestEventSchemaEvolution:
    """Event(**data) must not crash when JSONL contains unknown fields."""

    def test_unknown_fields_ignored(self, tmp_path):
        """Syncing a session with future fields should work, not crash."""
        from lib.storage import StorageManager

        base = tmp_path / "test-project"
        sm = StorageManager(base)
        try:
            # Write a JSONL event with an extra "future_field" key
            session_id = "test-schema-evolution"
            session_dir = base / "sessions"
            session_dir.mkdir(parents=True, exist_ok=True)
            event_with_extra = {
                "id": "evt_test001",
                "type": "UserPromptSubmit",
                "ts": "2026-04-03T10:00:00Z",
                "session_id": session_id,
                "agent_session_num": 0,
                "data": {"prompt": "hello"},
                "content": "hello",
                "future_field": "this_should_not_crash",
                "another_new_field": 42,
            }
            with open(session_dir / f"{session_id}.jsonl", "w") as f:
                f.write(json.dumps(event_with_extra) + "\n")

            # This should NOT raise TypeError
            synced = sm.sync_session(session_id)
            assert synced == 1

            # Verify the event was stored (without the extra fields)
            cursor = sm.sqlite.conn.execute("SELECT id, type, content FROM events WHERE session_id = ?", (session_id,))
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "evt_test001"
            assert row[1] == "UserPromptSubmit"
            assert row[2] == "hello"
        finally:
            sm.close()

    def test_known_fields_set(self):
        """_EVENT_FIELDS should contain all Event dataclass fields.

        task-508 Phase 1.4 additive fields: persona, agent_id, tool_name,
        tool_input_hash. Backfill targets duration_ms/tokens/cost which
        live as DB columns only (not Event fields yet).
        """
        from lib.storage import _EVENT_FIELDS

        expected = {
            "id", "session_id", "type", "ts", "agent_session_num",
            "data", "content", "images",
            # task-508 Phase 1.4 additive
            "persona", "agent_id", "tool_name", "tool_input_hash",
        }
        assert expected == _EVENT_FIELDS


# ── Cross-platform fcntl ────────────────────────────────────────────


class TestFcntlShim:
    """fcntl import should never fail, even conceptually on non-Linux."""

    def test_fcntl_available_in_log_event(self):
        """log_event.py should have a working fcntl (real or shim)."""
        from hooks import log_event

        assert hasattr(log_event, "fcntl")
        assert hasattr(log_event.fcntl, "flock")
        assert hasattr(log_event.fcntl, "LOCK_EX")
        assert hasattr(log_event.fcntl, "LOCK_UN")

    def test_fcntl_available_in_storage(self):
        """storage.py should have a working fcntl (real or shim)."""
        from lib import storage

        assert hasattr(storage, "fcntl")
        assert hasattr(storage.fcntl, "flock")


# ── Agent session number counting ───────────────────────────────────


class TestAgentSessionNum:
    """get_agent_session_num must parse JSON, not count substrings."""

    def test_counts_compact_events(self, tmp_path):
        """Should count events with source=compact in their data."""
        from hooks.log_event import get_agent_session_num

        session_path = tmp_path / "test.jsonl"
        events = [
            {"type": "SessionStart", "data": {"source": "startup"}},
            {"type": "UserPromptSubmit", "data": {"prompt": "hello"}},
            {"type": "PostCompact", "data": {"source": "compact", "summary": "..."}},
            {"type": "UserPromptSubmit", "data": {"prompt": "more"}},
        ]
        with open(session_path, "w") as f:
            for evt in events:
                f.write(json.dumps(evt) + "\n")

        # One compact event exists, not adding another
        assert get_agent_session_num(session_path, None) == 1

    def test_no_false_positive_from_content(self, tmp_path):
        """Content containing '"source": "compact"' should NOT be counted."""
        from hooks.log_event import get_agent_session_num

        session_path = tmp_path / "test.jsonl"
        # A user prompt that mentions compact in its text — not a real compact event
        events = [
            {
                "type": "UserPromptSubmit",
                "data": {"prompt": 'The event has "source": "compact" in its data field'},
            },
        ]
        with open(session_path, "w") as f:
            for evt in events:
                f.write(json.dumps(evt) + "\n")

        # Should be 0 — the compact string is inside a prompt, not a data.source field
        assert get_agent_session_num(session_path, None) == 0

    def test_empty_file(self, tmp_path):
        """Empty session file should return 0."""
        from hooks.log_event import get_agent_session_num

        session_path = tmp_path / "empty.jsonl"
        session_path.write_text("")
        assert get_agent_session_num(session_path, None) == 0

    def test_nonexistent_file(self, tmp_path):
        """Missing file should return 0 (or 1 if source is compact)."""
        from hooks.log_event import get_agent_session_num

        missing = tmp_path / "missing.jsonl"
        assert get_agent_session_num(missing, None) == 0
        assert get_agent_session_num(missing, "compact") == 1


# ── Module-level constants ──────────────────────────────────────────


class TestModuleConstants:
    """Constants that should be at module level, not inside functions."""

    def test_allowed_image_types_is_module_level(self):
        """ALLOWED_IMAGE_TYPES should be importable from hooks.log_event."""
        from hooks.log_event import ALLOWED_IMAGE_TYPES

        assert isinstance(ALLOWED_IMAGE_TYPES, set)
        assert "image/png" in ALLOWED_IMAGE_TYPES
        assert "image/jpeg" in ALLOWED_IMAGE_TYPES

    def test_heartbeat_names_is_tuple(self):
        """HEARTBEAT_NAMES should default to just 'logging' for public users."""
        from hooks.log_event import HEARTBEAT_NAMES

        assert isinstance(HEARTBEAT_NAMES, tuple)
        assert "logging" in HEARTBEAT_NAMES

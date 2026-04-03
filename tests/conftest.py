"""Shared test fixtures for claude-logging tests."""

import json
import sys
from pathlib import Path

import pytest

# Ensure plugin root is importable
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


SAMPLE_EVENTS = [
    # Session alpha — 2026-04-01
    {"id": "evt-a1", "session_id": "sess-alpha", "type": "SessionStart", "ts": "2026-04-01T10:00:00+00:00", "agent_session_num": 0, "data": {"source": "startup", "model": "claude-sonnet", "cwd": "/home/user/project"}, "content": "Session started (startup) - Model: claude-sonnet"},
    {"id": "evt-a2", "session_id": "sess-alpha", "type": "UserPromptSubmit", "ts": "2026-04-01T10:01:00+00:00", "agent_session_num": 0, "data": {"prompt": "Show me how authentication works"}, "content": "Show me how authentication works"},
    {"id": "evt-a3", "session_id": "sess-alpha", "type": "PreToolUse", "ts": "2026-04-01T10:01:05+00:00", "agent_session_num": 0, "data": {"tool_name": "Grep", "tool_input": {"pattern": "auth"}}, "content": "Searching for: auth"},
    {"id": "evt-a4", "session_id": "sess-alpha", "type": "PostToolUse", "ts": "2026-04-01T10:01:06+00:00", "agent_session_num": 0, "data": {"tool_name": "Grep"}, "content": "Search completed"},
    {"id": "evt-a5", "session_id": "sess-alpha", "type": "Stop", "ts": "2026-04-01T10:02:00+00:00", "agent_session_num": 0, "data": {}, "content": "Claude finished responding"},

    # Session beta — 2026-04-02
    {"id": "evt-b1", "session_id": "sess-beta", "type": "SessionStart", "ts": "2026-04-02T14:00:00+00:00", "agent_session_num": 0, "data": {"source": "resume"}, "content": "Session started (resume)"},
    {"id": "evt-b2", "session_id": "sess-beta", "type": "UserPromptSubmit", "ts": "2026-04-02T14:01:00+00:00", "agent_session_num": 0, "data": {"prompt": "Fix the database migration for PostgreSQL"}, "content": "Fix the database migration for PostgreSQL"},
    {"id": "evt-b3", "session_id": "sess-beta", "type": "PreToolUse", "ts": "2026-04-02T14:01:05+00:00", "agent_session_num": 0, "data": {"tool_name": "Read", "tool_input": {"file_path": "migrations/002.py"}}, "content": "Reading file: migrations/002.py"},
    {"id": "evt-b4", "session_id": "sess-beta", "type": "PostToolUse", "ts": "2026-04-02T14:01:06+00:00", "agent_session_num": 0, "data": {"tool_name": "Read"}, "content": "File read successfully"},
    {"id": "evt-b5", "session_id": "sess-beta", "type": "UserPromptSubmit", "ts": "2026-04-02T14:05:00+00:00", "agent_session_num": 0, "data": {"prompt": "Now run the tests"}, "content": "Now run the tests"},
    {"id": "evt-b6", "session_id": "sess-beta", "type": "PreToolUse", "ts": "2026-04-02T14:05:05+00:00", "agent_session_num": 0, "data": {"tool_name": "Bash", "tool_input": {"command": "pytest"}}, "content": "Running: pytest"},
    {"id": "evt-b7", "session_id": "sess-beta", "type": "PostToolUse", "ts": "2026-04-02T14:05:10+00:00", "agent_session_num": 0, "data": {"tool_name": "Bash", "tool_response": {"stdout": "5 passed"}}, "content": "Output: 5 passed"},
    {"id": "evt-b8", "session_id": "sess-beta", "type": "Stop", "ts": "2026-04-02T14:06:00+00:00", "agent_session_num": 0, "data": {}, "content": "Claude finished responding"},

    # Session gamma — 2026-04-03
    {"id": "evt-g1", "session_id": "sess-gamma", "type": "SessionStart", "ts": "2026-04-03T09:00:00+00:00", "agent_session_num": 0, "data": {"source": "startup"}, "content": "Session started (startup)"},
    {"id": "evt-g2", "session_id": "sess-gamma", "type": "UserPromptSubmit", "ts": "2026-04-03T09:01:00+00:00", "agent_session_num": 0, "data": {"prompt": "What did we discuss about caching?"}, "content": "What did we discuss about caching?"},
    {"id": "evt-g3", "session_id": "sess-gamma", "type": "Stop", "ts": "2026-04-03T09:02:00+00:00", "agent_session_num": 0, "data": {}, "content": "Claude finished responding"},
]


@pytest.fixture
def tmp_storage(tmp_path):
    """StorageManager seeded with 3 sessions and 16 events."""
    from lib.storage import StorageManager, Event, _EVENT_FIELDS

    sm = StorageManager(tmp_path / "logging")
    for evt_data in SAMPLE_EVENTS:
        event = Event(**{k: v for k, v in evt_data.items() if k in _EVENT_FIELDS})
        sm.jsonl.append_event(event)
    sm.sync_all()
    yield sm
    sm.close()


@pytest.fixture
def seeded_sqlite(tmp_storage):
    """SQLiteStorage with events already synced into FTS5."""
    return tmp_storage.sqlite


@pytest.fixture
def search_service(tmp_storage):
    """SearchService backed by seeded SQLite, no embeddings."""
    from lib.search import SearchService
    return SearchService(tmp_storage.sqlite)

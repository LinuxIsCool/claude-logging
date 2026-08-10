from __future__ import annotations

from web import claude_web_sessions
import sqlite3


def test_archived_conversation_maps_to_session_row(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_web_sessions, "PROJECTION", tmp_path)
    monkeypatch.setattr(claude_web_sessions, "_call", lambda *args: [{
        "rid": "orn:legion.claude-web.conversation:example",
        "uuid": "native-id", "title": "A web chat", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z", "message_count": 2,
        "opening_prompt": "Hello", "description": "Hi", "export_revision": "2026-08-09",
    }])

    row = claude_web_sessions.list_sessions(10, 0)[0]

    assert row["session_id"] == "claude-web:native-id"
    assert row["runtimes"] == ["claude-web"]
    assert row["source_kinds"] == ["archive"]
    assert row["source_rid"].startswith("orn:legion.claude-web.conversation:")


def test_archived_messages_map_to_deterministic_events(monkeypatch):
    record = {
        "rid": "orn:legion.claude-web.conversation:example", "uuid": "native-id",
        "title": "A web chat", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z", "message_count": 2,
        "messages": [
            {"uuid": "m1", "sender": "human", "text": "Hello", "created_at": "2026-01-01T00:00:00Z"},
            {"uuid": "m2", "sender": "assistant", "text": "Hi", "created_at": "2026-01-01T00:00:01Z"},
        ],
        "export_revision": "2026-08-09",
    }
    monkeypatch.setattr(claude_web_sessions, "_call", lambda *args: record)

    first = claude_web_sessions.get_transcript("claude-web:native-id", "clean")
    second = claude_web_sessions.get_transcript("claude-web:native-id", "clean")

    assert [event["type"] for event in first["events"]] == ["UserPromptSubmit", "AssistantResponse"]
    assert first["events"][0]["event_id"] == second["events"][0]["event_id"]
    assert all(event["source_kind"] == "archive" for event in first["events"])
    assert all(event["source_rid"] == record["rid"] for event in first["events"])


def test_search_uses_source_owned_fts_projection(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_web_sessions, "PROJECTION", tmp_path)
    con = sqlite3.connect(tmp_path / "search.db")
    con.executescript(
        "CREATE TABLE messages (event_id TEXT PRIMARY KEY, session_id TEXT, source_rid TEXT, type TEXT, ts TEXT, content TEXT);"
        "CREATE VIRTUAL TABLE messages_fts USING fts5(event_id UNINDEXED, content);"
        "INSERT INTO messages VALUES ('e1','s1','rid1','UserPromptSubmit','2026-01-01','universal adapter design');"
        "INSERT INTO messages_fts VALUES ('e1','universal adapter design');"
    )
    con.commit(); con.close()

    results = claude_web_sessions.search_sessions("adapter", "prompts", 10, 0)

    assert results[0]["session_id"] == "claude-web:s1"
    assert results[0]["source_kind"] == "archive"

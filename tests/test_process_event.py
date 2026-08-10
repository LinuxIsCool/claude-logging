"""Tests for process_event() — the core hot path that runs on every hook event."""

import json
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from hooks.log_event import process_event


@pytest.fixture
def event_env(tmp_path, monkeypatch):
    """Set up environment for process_event to write to tmp_path.

    Monkeypatches get_storage_path so all writes go to tmp_path/logging
    instead of ~/.claude/local/logging/{encoded}.
    """
    storage = tmp_path / "logging"
    monkeypatch.setattr("hooks.log_event.get_storage_path", lambda cwd=None: storage)
    monkeypatch.setattr("hooks.log_event.HEALTH_DIR", tmp_path / "health")
    return storage


def _read_session_events(storage: Path, session_id: str) -> list[dict]:
    """Read all events from a session JSONL file."""
    path = storage / "sessions" / f"{session_id}.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text().strip().split("\n"):
        if line.strip():
            events.append(json.loads(line))
    return events


class TestBasicEventCapture:
    """Every event type should produce a JSONL entry without crashing."""

    def test_session_start(self, event_env):
        data = {
            "session_id": "test-001",
            "data": {"source": "startup", "model": "sonnet", "cwd": str(event_env.parent)},
        }
        result = process_event("SessionStart", data)
        assert result["type"] == "SessionStart"
        assert result["session_id"] == "test-001"
        events = _read_session_events(event_env, "test-001")
        assert len(events) >= 1

    def test_user_prompt(self, event_env):
        data = {"session_id": "test-002", "data": {"prompt": "Hello Claude"}}
        result = process_event("UserPromptSubmit", data)
        assert result["content"] == "Hello Claude"
        events = _read_session_events(event_env, "test-002")
        assert any(e["type"] == "UserPromptSubmit" for e in events)
        import sqlite3

        con = sqlite3.connect(event_env / "db" / "logging.db")
        stored = con.execute(
            "SELECT type, content FROM events WHERE session_id = ?",
            ("test-002",),
        ).fetchall()
        con.close()
        assert ("UserPromptSubmit", "Hello Claude") in stored

    def test_pre_tool_use(self, event_env):
        data = {"session_id": "test-003", "data": {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x.py"}}}
        result = process_event("PreToolUse", data)
        assert "Reading file" in result["content"]

    def test_post_tool_use(self, event_env):
        data = {"session_id": "test-004", "data": {"tool_name": "Bash", "tool_response": {"stdout": "ok"}}}
        result = process_event("PostToolUse", data)
        assert "ok" in result["content"]

    def test_stop(self, event_env):
        """Stop without transcript_path should write normally."""
        data = {"session_id": "test-005", "data": {}}
        result = process_event("Stop", data)
        assert result["content"] == "Agent finished responding"

    def test_session_end(self, event_env):
        data = {"session_id": "test-006", "data": {}}
        result = process_event("SessionEnd", data)
        assert result["type"] == "SessionEnd"

    def test_notification(self, event_env):
        data = {"session_id": "test-007", "data": {"message": "Build passed"}}
        result = process_event("Notification", data)
        assert result["content"] == "Build passed"

    def test_pre_compact(self, event_env):
        data = {"session_id": "test-008", "data": {}}
        result = process_event("PreCompact", data)
        assert result["type"] == "PreCompact"

    def test_post_compact(self, event_env):
        data = {"session_id": "test-009", "data": {"summary": "Discussed auth patterns"}}
        result = process_event("PostCompact", data)
        assert "auth patterns" in result["content"]


class TestUnknownEventTypes:
    """Future Claude Code event types should be captured without crashing."""

    def test_unknown_type_captured(self, event_env):
        data = {"session_id": "test-future", "data": {"foo": "bar"}}
        result = process_event("BrandNewEventType", data)
        assert result["type"] == "BrandNewEventType"
        events = _read_session_events(event_env, "test-future")
        assert len(events) == 1

    def test_unknown_type_no_content(self, event_env):
        data = {"session_id": "test-future2", "data": {}}
        result = process_event("AnotherNew", data)
        # Unknown types get no content (extract_content returns None)
        assert result.get("content") is None


class TestStopWithTranscript:
    """Stop events with transcript_path trigger response capture."""

    def test_stop_captures_assistant_response(self, event_env):
        """When transcript exists, Stop should create an AssistantResponse event too."""
        session_id = "test-stop-transcript"
        # Create a fake transcript
        transcript_dir = event_env.parent / ".claude" / "projects" / "-test"
        transcript_dir.mkdir(parents=True)
        transcript = transcript_dir / f"{session_id}.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Here is my response"}]},
                }
            )
            + "\n"
        )

        data = {"session_id": session_id, "data": {"transcript_path": str(transcript)}}
        process_event("Stop", data)

        events = _read_session_events(event_env, session_id)
        types = [e["type"] for e in events]
        assert "Stop" in types
        assert "AssistantResponse" in types
        response_evt = next(e for e in events if e["type"] == "AssistantResponse")
        assert "Here is my response" in response_evt["content"]


class TestRuntimeProvenance:
    """Runtime adapters retain native identity without forking storage."""

    def test_codex_prompt_is_written_with_provenance(self, event_env):
        payload = {
            "_runtime": "codex",
            "_capture_source": "codex-hook",
            "hook_event_name": "UserPromptSubmit",
            "session_id": "codex-session",
            "turn_id": "turn-42",
            "model": "gpt-5.6",
            "permission_mode": "never",
            "prompt": "Port the logging plugin",
        }

        result = process_event("UserPromptSubmit", payload)

        assert result["runtime"] == "codex"
        assert result["turn_id"] == "turn-42"
        assert result["capture_source"] == "codex-hook"
        import sqlite3

        con = sqlite3.connect(event_env / "db" / "logging.db")
        stored = con.execute(
            """SELECT runtime, runtime_event, turn_id, capture_source, model,
                      permission_mode
               FROM events WHERE id = ?""",
            (result["id"],),
        ).fetchone()
        con.close()
        assert stored == (
            "codex",
            "UserPromptSubmit",
            "turn-42",
            "codex-hook",
            "gpt-5.6",
            "never",
        )

    def test_codex_stop_uses_inline_assistant_message(self, event_env):
        process_event(
            "Stop",
            {
                "_runtime": "codex",
                "session_id": "codex-stop",
                "turn_id": "turn-99",
                "last_assistant_message": "The Codex response is complete.",
            },
        )

        events = _read_session_events(event_env, "codex-stop")
        response = next(event for event in events if event["type"] == "AssistantResponse")
        assert response["content"] == "The Codex response is complete."
        assert response["runtime"] == "codex"
        assert response["turn_id"] == "turn-99"


class TestSubagentStopEnrichment:
    """SubagentStop with transcript should extract full content."""

    def test_enriches_with_transcript(self, event_env):
        session_id = "test-subagent"
        # Create a fake subagent transcript
        transcript = event_env.parent / "subagent-transcript.jsonl"
        lines = [
            json.dumps({"type": "user", "message": {"content": "Research caching strategies"}}),
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-04-03T10:00:00Z",
                    "message": {
                        "model": "claude-sonnet-4-6",
                        "content": [
                            {"type": "text", "text": "Here are three caching strategies worth considering."},
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "cache.py"}},
                        ],
                        "usage": {"input_tokens": 500, "output_tokens": 200},
                    },
                }
            ),
        ]
        transcript.write_text("\n".join(lines) + "\n")

        data = {
            "session_id": session_id,
            "data": {
                "agent_type": "Explore",
                "agent_transcript_path": str(transcript),
            },
        }
        result = process_event("SubagentStop", data)

        # Content should contain the full assistant text, not just "Agent finished"
        assert "caching strategies" in result["content"]
        # Structured metadata should be in data
        summary = result["data"]["transcript_summary"]
        assert summary["model"] == "sonnet"
        assert "Read" in summary["tool_names"]
        assert summary["turn_count"] == 1
        assert summary["token_usage"]["input_tokens"] == 500


class TestEventStructure:
    """All events should have required fields."""

    def test_has_required_fields(self, event_env):
        data = {"session_id": "test-fields", "data": {"prompt": "test"}}
        result = process_event("UserPromptSubmit", data)
        assert "id" in result
        assert "type" in result
        assert "ts" in result
        assert "session_id" in result
        assert "agent_session_num" in result
        assert "data" in result

    def test_id_is_unique(self, event_env):
        ids = set()
        for i in range(5):
            result = process_event("Stop", {"session_id": f"test-unique-{i}", "data": {}})
            ids.add(result["id"])
        assert len(ids) == 5

    def test_timestamp_is_iso(self, event_env):
        result = process_event("Stop", {"session_id": "test-ts", "data": {}})
        ts = result["ts"]
        assert "T" in ts
        assert "+" in ts or "Z" in ts


class TestAgentSessionNum:
    """agent_session_num should track context resets within a session."""

    def test_first_event_is_zero(self, event_env):
        result = process_event("UserPromptSubmit", {"session_id": "test-asn", "data": {"prompt": "hi"}})
        assert result["agent_session_num"] == 0

    def test_increments_on_compact(self, event_env):
        sid = "test-asn-compact"
        process_event("UserPromptSubmit", {"session_id": sid, "data": {"prompt": "before compact"}})
        result = process_event("PostCompact", {"session_id": sid, "data": {"source": "compact", "summary": "stuff"}})
        assert result["agent_session_num"] == 1


class TestContentBlockPrompts:
    """UserPromptSubmit with content blocks (text + images)."""

    def test_text_blocks_extracted(self, event_env):
        data = {
            "session_id": "test-blocks",
            "data": {
                "prompt": [
                    {"type": "text", "text": "Look at this"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": ""}},
                    {"type": "text", "text": "What do you see?"},
                ],
            },
        }
        result = process_event("UserPromptSubmit", data)
        # Text should be extracted, empty image ignored
        assert "Look at this" in result["content"]
        assert "What do you see?" in result["content"]

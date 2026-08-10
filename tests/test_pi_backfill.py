import json

from lib.pi_backfill import events_from_session


def test_pi_tree_session_preserves_branch_and_rich_messages(tmp_path):
    path = tmp_path / "pi.jsonl"
    rows = [
        {"type":"session","version":3,"id":"pi-1","timestamp":"2026-01-01T00:00:00Z","cwd":"/work"},
        {"type":"model_change","id":"m","parentId":None,"timestamp":"2026-01-01T00:00:01Z","provider":"openai","modelId":"gpt-5"},
        {"type":"message","id":"u","parentId":"m","timestamp":"2026-01-01T00:00:02Z","message":{"role":"user","content":"Hello"}},
        {"type":"message","id":"a","parentId":"u","timestamp":"2026-01-01T00:00:03Z","message":{"role":"assistant","content":[{"type":"thinking","thinking":"Plan"},{"type":"toolCall","id":"tc","name":"read","arguments":{}},{"type":"text","text":"Done"}]}},
        {"type":"message","id":"r","parentId":"a","timestamp":"2026-01-01T00:00:04Z","message":{"role":"toolResult","toolName":"read","content":[{"type":"text","text":"result"}]}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows))
    session = events_from_session(path)
    assert session.session_id == "pi-1" and session.version == 3
    assert [e.type for e in session.events] == ["SessionStart","ModelChange","UserPromptSubmit","AssistantResponse","Reasoning","PreToolUse","PostToolUse"]
    assert all(e.runtime == "pi" and e.source_kind == "backfill" for e in session.events)
    assert session.events[2].data["parent_id"] == "m"


def test_pi_event_ids_are_stable(tmp_path):
    path = tmp_path / "pi.jsonl"
    path.write_text(json.dumps({"type":"session","version":3,"id":"pi-1","timestamp":"2026-01-01T00:00:00Z","cwd":"/work"}))
    assert [e.id for e in events_from_session(path).events] == [e.id for e in events_from_session(path).events]


def test_family_archive_preserves_runtime_identity(tmp_path):
    path = tmp_path / "prime.jsonl"
    path.write_text("\n".join([
        json.dumps({"type":"session","version":3,"id":"prime-1","timestamp":"2026-01-01T00:00:00Z","cwd":"/work"}),
        json.dumps({"type":"message","id":"u","parentId":None,"timestamp":"2026-01-01T00:00:01Z","message":{"role":"user","content":"Hello"}}),
    ]))
    session = events_from_session(path, runtime="prime-agent")
    assert [event.runtime for event in session.events] == ["prime-agent", "prime-agent"]
    assert all(event.capture_source == "prime-agent-session-backfill" for event in session.events)
    assert all(event.id.startswith("evt_prime_agent") for event in session.events)

import json

from lib.codex_backfill import events_from_rollout


def test_codex_rollout_maps_rich_native_records(tmp_path):
    path = tmp_path / "rollout.jsonl"
    rows = [
        {"timestamp":"2026-01-01T00:00:00Z","type":"session_meta","payload":{"id":"s1","cwd":"/work"}},
        {"timestamp":"2026-01-01T00:00:01Z","type":"turn_context","payload":{"turn_id":"t1","model":"gpt-5","approval_policy":"never"}},
        {"timestamp":"2026-01-01T00:00:02Z","type":"response_item","payload":{"type":"message","id":"u1","role":"user","content":[{"type":"input_text","text":"Build it"}]}},
        {"timestamp":"2026-01-01T00:00:03Z","type":"response_item","payload":{"type":"function_call","id":"c1","call_id":"call","name":"exec_command","arguments":"{}"}},
        {"timestamp":"2026-01-01T00:00:04Z","type":"response_item","payload":{"type":"message","id":"a1","role":"assistant","content":[{"type":"output_text","text":"Done"}]}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows))

    rollout = events_from_rollout(path)

    assert rollout.session_id == "s1"
    assert rollout.cwd == "/work"
    assert [e.type for e in rollout.events] == ["SessionStart", "UserPromptSubmit", "PreToolUse", "AssistantResponse"]
    assert all(e.runtime == "codex" and e.source_kind == "backfill" for e in rollout.events)
    assert rollout.events[-1].model == "gpt-5"
    assert rollout.events[-1].permission_mode == "never"


def test_codex_rollout_ids_are_deterministic(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(json.dumps({"timestamp":"2026-01-01T00:00:00Z","type":"session_meta","payload":{"id":"s1","cwd":"/work"}}))
    first = events_from_rollout(path)
    second = events_from_rollout(path)
    assert [e.id for e in first.events] == [e.id for e in second.events]

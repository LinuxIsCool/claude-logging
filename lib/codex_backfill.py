"""Deterministic backfill adapter for native Codex rollout JSONL files."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from lib.storage import Event

SOURCE = "codex-rollout-backfill"


@dataclass
class CodexRollout:
    session_id: str
    cwd: str
    events: list[Event]


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict):
            value = block.get("text") or block.get("input_text") or block.get("output_text")
            if value:
                parts.append(str(value))
    return "\n".join(parts).strip()


def _id(session_id: str, native_id: str, kind: str, index: int) -> str:
    raw = f"{session_id}:{native_id}:{kind}:{index}"
    return "evt_cx" + hashlib.sha1(raw.encode()).hexdigest()[:12]


def events_from_rollout(path: Path) -> CodexRollout:
    records = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)

    meta = next((r.get("payload", {}) for r in records if r.get("type") == "session_meta"), {})
    session_id = meta.get("id") or meta.get("session_id") or path.stem.rsplit("-", 5)[-1]
    cwd = meta.get("cwd") or ""
    model = None
    permission = None
    current_turn = None
    events = []

    for index, record in enumerate(records):
        payload = record.get("payload") or {}
        outer = record.get("type")
        native = payload.get("type") or outer
        ts = record.get("timestamp") or payload.get("created_at") or meta.get("timestamp") or ""
        if outer == "turn_context":
            model = payload.get("model") or model
            permission = payload.get("approval_policy") or permission
            current_turn = payload.get("turn_id") or current_turn
            continue

        event_type = None
        content = ""
        tool_name = None
        if outer == "session_meta":
            event_type = "SessionStart"
            content = "Codex session started"
        elif outer == "response_item" and native == "message":
            role = payload.get("role")
            event_type = "UserPromptSubmit" if role == "user" else "AssistantResponse"
            content = _text(payload.get("content"))
            if not content:
                continue
        elif outer == "response_item" and native in ("function_call", "custom_tool_call"):
            event_type = "PreToolUse"
            tool_name = payload.get("name")
            content = f"Tool: {tool_name or 'unknown'}"
        elif outer == "response_item" and native in ("function_call_output", "custom_tool_call_output"):
            event_type = "PostToolUse"
            content = _text(payload.get("output")) or str(payload.get("output") or "")
        elif outer == "response_item" and native == "reasoning":
            event_type = "Reasoning"
            content = _text(payload.get("summary"))
        elif outer == "event_msg" and native == "task_started":
            event_type = "TurnStart"
            current_turn = payload.get("turn_id") or current_turn
        elif outer == "event_msg" and native == "task_complete":
            event_type = "TurnEnd"
            current_turn = payload.get("turn_id") or current_turn
        elif outer == "event_msg" and native == "token_count":
            event_type = "TokenUsage"
        else:
            continue

        native_id = str(payload.get("id") or payload.get("call_id") or current_turn or index)
        events.append(Event(
            id=_id(session_id, native_id, native, index), session_id=session_id,
            type=event_type, ts=ts, data={**payload, "_source": SOURCE},
            content=content, tool_name=tool_name, runtime="codex",
            runtime_event=native, turn_id=payload.get("turn_id") or current_turn,
            capture_source=SOURCE, source_kind="backfill", model=model,
            permission_mode=permission,
        ))
    return CodexRollout(session_id=session_id, cwd=cwd, events=events)

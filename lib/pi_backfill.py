"""Archive adapter for Pi's versioned, tree-structured session JSONL."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from lib.storage import Event

SOURCE = "pi-session-backfill"


@dataclass
class PiSession:
    session_id: str
    cwd: str
    version: int
    events: list[Event]


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text") or "") for block in content
        if isinstance(block, dict) and block.get("type") in ("text", "input_text", "output_text")
    ).strip()


def _event(session_id: str, entry: dict, index: int, kind: str, content: str = "", *, runtime: str = "pi", **fields) -> Event:
    native_id = entry.get("id") or str(index)
    stable = hashlib.sha1(f"{session_id}:{native_id}:{kind}:{index}".encode()).hexdigest()[:12]
    source = f"{runtime}-session-backfill"
    prefix = runtime.replace("-", "_")
    return Event(
        id=f"evt_{prefix}{stable}", session_id=session_id, type=kind,
        ts=entry.get("timestamp") or "", content=content,
        data={**entry, "_source": source, "parent_id": entry.get("parentId")},
        runtime=runtime, runtime_event=entry.get("type") or kind,
        capture_source=source, source_kind="backfill", **fields,
    )


def events_from_session(path: Path, runtime: str = "pi") -> PiSession:
    entries = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                entries.append(value)
    header = next((e for e in entries if e.get("type") == "session"), {})
    session_id = header.get("id") or path.stem
    events = [_event(session_id, header, 0, "SessionStart", f"{runtime} session started", runtime=runtime)]
    model = None
    for index, entry in enumerate(entries[1:], 1):
        entry_type = entry.get("type")
        if entry_type == "model_change":
            model = entry.get("modelId") or model
            events.append(_event(session_id, entry, index, "ModelChange", model or "", runtime=runtime, model=model))
        elif entry_type == "thinking_level_change":
            events.append(_event(session_id, entry, index, "ThinkingLevelChange", str(entry.get("thinkingLevel") or ""), runtime=runtime, model=model))
        elif entry_type == "message":
            message = entry.get("message") or {}
            role = message.get("role")
            content = message.get("content")
            if role == "user":
                text = _text(content)
                if text:
                    events.append(_event(session_id, entry, index, "UserPromptSubmit", text, runtime=runtime, model=model))
            elif role == "assistant":
                text = _text(content)
                if text:
                    events.append(_event(session_id, entry, index, "AssistantResponse", text, runtime=runtime, model=message.get("model") or model))
                if isinstance(content, list):
                    for block_index, block in enumerate(content):
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "thinking":
                            events.append(_event(session_id, {**entry, "id": f"{entry.get('id')}:{block_index}"}, index, "Reasoning", block.get("thinking") or block.get("text") or "", runtime=runtime, model=model))
                        elif block.get("type") in ("toolCall", "tool_call", "tool_use"):
                            name = block.get("name") or block.get("toolName")
                            events.append(_event(session_id, {**entry, "id": block.get("id") or f"{entry.get('id')}:{block_index}"}, index, "PreToolUse", f"Tool: {name}", runtime=runtime, tool_name=name, model=model))
            elif role in ("toolResult", "tool_result"):
                name = message.get("toolName")
                events.append(_event(session_id, entry, index, "PostToolUse", _text(content), runtime=runtime, tool_name=name, model=model))
        elif entry_type == "compaction":
            events.append(_event(session_id, entry, index, "PostCompact", entry.get("summary") or "", runtime=runtime, model=model))
        elif entry_type == "branch_summary":
            events.append(_event(session_id, entry, index, "BranchSummary", entry.get("summary") or "", runtime=runtime, model=model))
        elif entry_type == "session_info":
            events.append(_event(session_id, entry, index, "SessionInfo", entry.get("name") or "", runtime=runtime, model=model))
        elif entry_type == "custom_message" and entry.get("display", True):
            events.append(_event(session_id, entry, index, "CustomMessage", _text(entry.get("content")), runtime=runtime, model=model))
    return PiSession(session_id, header.get("cwd") or "", int(header.get("version") or 1), events)

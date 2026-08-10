"""Read-only projection of Hermes' SQLite session store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from lib.storage import Event

RUNTIME = "hermes"
SOURCE = "hermes-state-backfill"


@dataclass
class HermesSession:
    session_id: str
    cwd: str
    parent_session_id: str | None
    events: list[Event]


def _ts(value: float | int | None) -> str:
    return datetime.fromtimestamp(float(value or 0), tz=timezone.utc).isoformat()


def _event(session: sqlite3.Row, native: str, kind: str, ts: float, content: str = "", **fields) -> Event:
    stable = hashlib.sha1(f"{session['id']}:{native}:{kind}".encode()).hexdigest()[:14]
    data = fields.pop("data", {})
    return Event(
        id=f"evt_hermes{stable}", session_id=session["id"], type=kind, ts=_ts(ts),
        content=content or "", runtime=RUNTIME, runtime_event=kind,
        capture_source=SOURCE, source_kind="backfill", model=session["model"],
        data={**data, "_source": SOURCE, "parent_session_id": session["parent_session_id"]},
        **fields,
    )


def _json(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return fallback


def sessions_from_db(path: Path) -> list[HermesSession]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        sessions = con.execute("SELECT * FROM sessions ORDER BY started_at").fetchall()
        projected = []
        for session in sessions:
            events = [_event(
                session, "session:start", "SessionStart", session["started_at"],
                session["title"] or f"Hermes {session['source']} session",
                tokens_in=session["input_tokens"], tokens_out=session["output_tokens"],
                cost_usd=session["actual_cost_usd"] if session["actual_cost_usd"] is not None else session["estimated_cost_usd"],
                data={"source": session["source"], "platform": session["source"], "origin": _json(session["origin_json"], {})},
            )]
            messages = con.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY timestamp,id", (session["id"],)
            ).fetchall()
            for message in messages:
                native = f"message:{message['id']}"
                role = message["role"]
                content = message["content"] or ""
                if role == "user":
                    events.append(_event(session, native, "UserPromptSubmit", message["timestamp"], content, data={"message_id": message["id"], "role": role}))
                elif role == "assistant":
                    reasoning = message["reasoning_content"] or message["reasoning"] or ""
                    if reasoning:
                        events.append(_event(session, native + ":reasoning", "Reasoning", message["timestamp"], reasoning, data={"message_id": message["id"], "reasoning_details": _json(message["reasoning_details"], message["reasoning_details"])}))
                    if content:
                        events.append(_event(session, native, "AssistantResponse", message["timestamp"], content, tokens_out=message["token_count"], data={"message_id": message["id"], "finish_reason": message["finish_reason"]}))
                    for index, call in enumerate(_json(message["tool_calls"], [])):
                        if not isinstance(call, dict):
                            continue
                        function = call.get("function") or {}
                        name = function.get("name") or call.get("name")
                        call_id = call.get("id") or call.get("call_id") or f"{message['id']}:{index}"
                        arguments = _json(function.get("arguments"), function.get("arguments"))
                        events.append(_event(session, native + f":tool:{call_id}", "PreToolUse", message["timestamp"], f"Tool: {name}", tool_name=name, data={"tool_name": name, "tool_input": arguments, "tool_use_id": call_id, "message_id": message["id"]}))
                elif role == "tool":
                    events.append(_event(session, native, "PostToolUse", message["timestamp"], content, tool_name=message["tool_name"], data={"tool_name": message["tool_name"], "tool_response": content, "tool_use_id": message["tool_call_id"], "effect_disposition": message["effect_disposition"]}))
            if session["ended_at"] is not None:
                events.append(_event(session, "session:end", "SessionEnd", session["ended_at"], session["end_reason"] or "Hermes session ended", data={"end_reason": session["end_reason"]}))
            projected.append(HermesSession(session["id"], session["cwd"] or "", session["parent_session_id"], events))
        return projected
    finally:
        con.close()

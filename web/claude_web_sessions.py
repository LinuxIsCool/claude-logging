"""Read-only Claude Web conversation adapter for the unified Sessions UI."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sqlite3
from pathlib import Path
from typing import Any


ACCESSOR = Path(__file__).resolve().parents[1].parent / "claude-claude-web" / "scripts" / "conversation_accessor.py"
PROJECTION = Path.home() / ".claude" / "local" / "claude-claude-web" / "projection" / "conversations"
PREFIX = "claude-web:"


def _call(*args: str) -> Any:
    if not ACCESSOR.exists():
        return None
    result = subprocess.run(
        ["uv", "run", str(ACCESSOR), *args], capture_output=True, text=True,
        timeout=15, check=False,
    )
    if result.returncode not in (0, 1) or not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def list_sessions(limit: int, offset: int) -> list[dict[str, Any]]:
    index = PROJECTION / "index.json"
    if index.exists():
        try:
            rows = json.loads(index.read_text())[offset:offset + limit]
        except (OSError, json.JSONDecodeError):
            rows = _call("list", "--limit", str(limit), "--offset", str(offset)) or []
    else:
        rows = _call("list", "--limit", str(limit), "--offset", str(offset)) or []
    return [
        {
            "session_id": PREFIX + row["uuid"],
            "source_session_id": row["uuid"],
            "source_rid": row["rid"],
            "project_slug": "claude-web",
            "project_slugs": ["claude-web"],
            "title": row["title"],
            "description": row.get("description") or row.get("summary") or "",
            "opening_prompt": row.get("opening_prompt") or "",
            "started_at": row.get("created_at"),
            "updated_at": row.get("updated_at") or row.get("created_at"),
            "latest_message_at": row.get("updated_at") or row.get("created_at"),
            "event_count": row.get("message_count", 0),
            "runtimes": ["claude-web"],
            "source_kinds": ["archive"],
            "tags": ["Claude Web"],
            "export_revision": row.get("export_revision"),
        }
        for row in rows
    ]


def get_transcript(session_id: str, mode: str) -> dict[str, Any] | None:
    if not session_id.startswith(PREFIX):
        return None
    native_id = session_id[len(PREFIX):]
    detail = PROJECTION / f"{native_id}.json"
    try:
        row = json.loads(detail.read_text()) if detail.exists() else _call("get", native_id)
    except (OSError, json.JSONDecodeError):
        row = _call("get", native_id)
    if not row:
        return {"error": f"session not found: {session_id}"}
    events = []
    artifacts = []
    for index, message in enumerate(row.get("messages", [])):
        sender = message.get("sender")
        event_type = "UserPromptSubmit" if sender == "human" else "AssistantResponse"
        if mode == "clean" and event_type not in ("UserPromptSubmit", "AssistantResponse"):
            continue
        identity = message.get("uuid") or str(index)
        event_id = hashlib.sha256(f"{row['rid']}:{identity}".encode()).hexdigest()
        message_artifacts = [
            {**artifact, "kind": kind, "message_id": identity}
            for kind, values in (("attachment", message.get("attachments") or []), ("file", message.get("files") or []))
            for artifact in values if isinstance(artifact, dict)
        ]
        artifacts.extend(message_artifacts)
        events.append({
            "event_id": event_id,
            "type": event_type,
            "ts": message.get("created_at") or row.get("created_at"),
            "content": message.get("text") or "",
            "content_truncated": False,
            "runtime": "claude-web",
            "runtime_event": f"message:{sender or 'unknown'}",
            "turn_id": identity,
            "capture_source": "claude-web-export",
            "source_kind": "archive",
            "model": None,
            "permission_mode": None,
            "persona": None,
            "agent_id": None,
            "tool_name": None,
            "data": None if mode == "explore" else {"source_rid": row["rid"], "message": message},
            "data_loaded": mode != "explore",
            "project_slug": "claude-web",
            "source_rid": row["rid"],
            "artifacts": message_artifacts,
        })
    opening = next((e["content"] for e in events if e["type"] == "UserPromptSubmit"), "")
    return {
        "session_id": session_id,
        "source_session_id": row["uuid"],
        "source_rid": row["rid"],
        "project_slug": "claude-web",
        "project_slugs": ["claude-web"],
        "mode": mode,
        "event_count": len(events),
        "total_event_count": row.get("message_count", len(events)),
        "started_at": row.get("created_at"),
        "updated_at": row.get("updated_at") or row.get("created_at"),
        "runtimes": ["claude-web"],
        "source_kinds": ["archive"],
        "models": [],
        "persona": None,
        "opening_prompt": opening,
        "title": row.get("title") or "Untitled",
        "export_revision": row.get("export_revision"),
        "artifacts": artifacts,
        "events": events,
    }


def search_sessions(query: str, mode: str, limit: int, offset: int) -> list[dict[str, Any]]:
    """Search the source-owned public FTS projection."""
    db = PROJECTION / "search.db"
    if not db.exists():
        return []
    tokens = query.split()
    fts_query = " ".join('"' + token.replace('"', '""') + '"' for token in tokens)
    type_clause = "AND m.type = 'UserPromptSubmit'" if mode == "prompts" else ""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT m.event_id, m.session_id, m.source_rid, m.type, m.ts, "
            "snippet(messages_fts, 1, '[', ']', '…', 24) AS preview "
            "FROM messages_fts JOIN messages m ON m.event_id=messages_fts.event_id "
            f"WHERE messages_fts MATCH ? {type_clause} ORDER BY m.ts DESC LIMIT ? OFFSET ?",
            (fts_query, limit, offset),
        ).fetchall()
        return [{
            "id": row["event_id"], "event_id": row["event_id"],
            "session_id": PREFIX + row["session_id"], "source_rid": row["source_rid"],
            "project_slug": "claude-web", "type": row["type"], "ts": row["ts"],
            "persona": None, "preview": row["preview"], "content": row["preview"],
            "has_full": True, "runtime": "claude-web", "source_kind": "archive",
        } for row in rows]
    finally:
        con.close()

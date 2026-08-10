"""Read-only native session graph projections for the Sessions UI."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

PI_SESSIONS = Path.home() / ".pi" / "agent" / "sessions"
FAMILY_SESSIONS = {
    "pi": PI_SESSIONS,
    "prime-agent": Path.home() / ".prime" / "agent" / "sessions",
    "omp": Path.home() / ".omp" / "agent" / "sessions",
}
HERMES_DB = Path.home() / ".hermes" / "state.db"


def _family_graph(session_id: str, runtime: str) -> dict[str, Any] | None:
    root = PI_SESSIONS if runtime == "pi" else FAMILY_SESSIONS.get(runtime)
    if root is None or not root.exists():
        return None
    path = next(root.rglob(f"*{session_id}*.jsonl"), None)
    if path is None:
        return None
    nodes = []
    title = None
    version = 1
    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "session":
                version = int(entry.get("version") or 1)
                continue
            if entry.get("type") == "session_info" and entry.get("name"):
                title = entry["name"]
            if entry.get("type") == "title" and entry.get("title"):
                title = entry["title"]
            node_id = entry.get("id")
            if not node_id:
                continue
            message = entry.get("message") or {}
            role = message.get("role")
            label = entry.get("type") or "entry"
            if role:
                label = f"{label}:{role}"
            if entry.get("type") == "session_info" and entry.get("name"):
                label = f"name: {entry['name']}"
            nodes.append({
                "id": node_id, "parent_id": entry.get("parentId"),
                "type": entry.get("type"), "role": role,
                "timestamp": entry.get("timestamp"), "label": label,
            })
    by_id = {node["id"]: node for node in nodes}
    for node in nodes:
        depth = 0
        parent = node.get("parent_id")
        seen = set()
        while parent and parent in by_id and parent not in seen:
            seen.add(parent); depth += 1; parent = by_id[parent].get("parent_id")
        node["depth"] = depth
    parents = {node.get("parent_id") for node in nodes if node.get("parent_id")}
    leaves = [node["id"] for node in nodes if node["id"] not in parents]
    total_nodes = len(nodes)
    total_leaves = len(leaves)
    if total_nodes > 300:
        child_counts: dict[str, int] = {}
        for node in nodes:
            parent = node.get("parent_id")
            if parent:
                child_counts[parent] = child_counts.get(parent, 0) + 1
        keep = set(range(min(20, total_nodes)))
        keep.update(range(max(20, total_nodes - 200), total_nodes))
        for index, node in enumerate(nodes):
            if node["id"] in leaves or child_counts.get(node["id"], 0) > 1 or child_counts.get(node.get("parent_id"), 0) > 1:
                keep.add(index)
        ordered = sorted(keep)
        if len(ordered) > 300:
            ordered = ordered[:20] + ordered[-280:]
        nodes = [{**nodes[index], "ordinal": index} for index in ordered]
        visible_ids = {node["id"] for node in nodes}
        leaves = [leaf for leaf in leaves if leaf in visible_ids]
    return {"runtime": runtime, "kind": "message_tree", "version": version, "title": title, "path": str(path), "nodes": nodes, "leaves": leaves, "total_nodes": total_nodes, "total_leaves": total_leaves, "collapsed_nodes": total_nodes - len(nodes)}


def _hermes_graph(session_id: str) -> dict[str, Any] | None:
    if not HERMES_DB.exists():
        return None
    con = sqlite3.connect(f"file:{HERMES_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id,parent_session_id,title,source,started_at FROM sessions"
        ).fetchall()
    finally:
        con.close()
    by_id = {row["id"]: dict(row) for row in rows}
    if session_id not in by_id:
        return None
    included = {session_id}
    cursor = session_id
    while by_id.get(cursor, {}).get("parent_session_id") in by_id:
        cursor = by_id[cursor]["parent_session_id"]
        included.add(cursor)
    changed = True
    while changed:
        changed = False
        for row in rows:
            if row["parent_session_id"] in included and row["id"] not in included:
                included.add(row["id"]); changed = True
    nodes = []
    for row in sorted((by_id[node_id] for node_id in included), key=lambda item: item.get("started_at") or 0):
        depth = 0
        parent = row.get("parent_session_id")
        seen = set()
        while parent in included and parent not in seen:
            seen.add(parent); depth += 1; parent = by_id[parent].get("parent_session_id")
        label = row.get("title") or f"{row.get('source') or 'hermes'} · {row['id'][:12]}"
        nodes.append({
            "id": row["id"], "parent_id": row.get("parent_session_id"), "depth": depth,
            "type": "session", "role": None, "timestamp": row.get("started_at"),
            "label": label, "target_session_id": row["id"], "current": row["id"] == session_id,
        })
    parents = {node["parent_id"] for node in nodes if node.get("parent_id") in included}
    leaves = [node["id"] for node in nodes if node["id"] not in parents]
    return {"runtime": "hermes", "kind": "session_lineage", "version": 1, "title": by_id[session_id].get("title"), "nodes": nodes, "leaves": leaves}


def session_graph(session_id: str, runtime: str = "pi") -> dict[str, Any] | None:
    if runtime == "hermes":
        return _hermes_graph(session_id)
    return _family_graph(session_id, runtime)

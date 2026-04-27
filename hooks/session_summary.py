#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pyyaml",
# ]
# ///
"""
Session Summary Hook — KEYSTONE for Legion's recursive self-improvement loop.

Triggered on Stop event AFTER log_event.py has recorded the Stop record.

Reads all events for the current session from logging.db, computes structured
summary, and writes:
  1. ~/.claude/local/sessions/{YYYY-MM-DD}/{session-id}/summary.yaml
  2. session_summaries SQLite row (source='stop_hook', upsert)

Optionally calls TELUS for narrative summary (gated by event_count threshold).

Reference: ~/.claude/local/research/2026/04/27/stop-hook-session-summary-keystone/DESIGN.md

Usage:
    echo '{"session_id":"...","cwd":"/path"}' | uv run hooks/session_summary.py
"""

import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# Ensure lib is importable
_PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from lib import encode_project_path  # noqa: E402

SCHEMA_VERSION = 1
NARRATIVE_MIN_EVENTS = 10        # Skip narrative for trivial sessions
NARRATIVE_MIN_DURATION_SEC = 60  # Skip if Stop fires <60s after start (test runs)
NARRATIVE_MAX_OUTPUT_TOKENS = 80
SESSIONS_ROOT = Path.home() / ".claude" / "local" / "sessions"


def _read_input() -> dict[str, Any]:
    """Read Stop event payload from stdin."""
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _resolve_db_path(cwd: str) -> Path | None:
    """Locate logging.db for the project containing cwd."""
    encoded = encode_project_path(cwd)
    db = Path.home() / ".claude" / "local" / "logging" / encoded / "db" / "logging.db"
    return db if db.exists() else None


def _load_session_events(db: Path, session_id: str) -> list[dict[str, Any]]:
    """Pull all events for this session, ordered by timestamp."""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, type, ts, agent_session_num, data, content "
        "FROM events WHERE session_id = ? ORDER BY ts",
        (session_id,),
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "type": r["type"],
            "ts": r["ts"],
            "agent_session_num": r["agent_session_num"] or 0,
            "data": json.loads(r["data"]) if r["data"] else {},
            "content": r["content"] or "",
        }
        for r in rows
    ]


def _compute_summary(session_id: str, cwd: str, events: list[dict]) -> dict[str, Any]:
    """Build the structured summary dict from event stream."""
    if not events:
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "project": cwd,
            "event_count": 0,
            "completion_state": "unknown",
        }

    started_at = events[0]["ts"]
    ended_at = events[-1]["ts"]
    duration_seconds = _compute_duration(started_at, ended_at)

    # Count user prompts
    prompts = [e for e in events if e["type"] == "UserPromptSubmit"]
    prompt_text = "\n".join(e["content"] for e in prompts if e.get("content"))
    tokens_in_estimate = len(prompt_text) // 4  # crude approximation

    # Tools used (frequency map)
    tool_uses = [e for e in events if e["type"] == "PreToolUse"]
    tool_counter: Counter = Counter()
    for e in tool_uses:
        tool = e["data"].get("tool_name") or e["data"].get("tool", "unknown")
        tool_counter[tool] += 1

    # Artifacts (Write + Edit operations from PostToolUse)
    artifacts = []
    for e in events:
        if e["type"] != "PostToolUse":
            continue
        tool = e["data"].get("tool_name") or e["data"].get("tool", "")
        if tool not in {"Write", "Edit", "NotebookEdit"}:
            continue
        path = (
            e["data"].get("tool_input", {}).get("file_path")
            or e["data"].get("input", {}).get("file_path")
        )
        if path:
            op = "edit" if tool == "Edit" else "write"
            artifacts.append({"path": path, "op": op})

    # Backgrounded agents dispatched
    agents_dispatched = []
    for e in events:
        if e["type"] != "PreToolUse":
            continue
        tool = e["data"].get("tool_name") or ""
        if tool != "Agent":
            continue
        ti = e["data"].get("tool_input", {})
        agents_dispatched.append({
            "subagent_type": ti.get("subagent_type"),
            "description": ti.get("description"),
            "run_in_background": ti.get("run_in_background", False),
        })

    # Errors
    errors = [e for e in events if e["type"] == "PostToolUseFailure"]

    # Completion state heuristic (refine in v0.1+)
    completion = _guess_completion_state(events)

    # Themes (simple tag extraction from prompts; refine later)
    themes = _extract_themes(prompts)

    # Tokens out estimate (very crude — sum content lengths of assistant content)
    out_text = "\n".join(
        e.get("content", "") for e in events if e["type"] in {"Stop", "SubagentStop"}
    )
    tokens_out_estimate = len(out_text) // 4

    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "agent_session_num": events[-1]["agent_session_num"],
        "project": cwd,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": duration_seconds,
        "event_count": len(events),
        "prompts": {
            "user_count": len(prompts),
            "tokens_in_estimate": tokens_in_estimate,
            "tokens_out_estimate": tokens_out_estimate,
        },
        "tools_used": dict(tool_counter.most_common()),
        "artifacts": artifacts,
        "agents_dispatched": agents_dispatched,
        "errors": {"count": len(errors)},
        "completion_state": completion,
        "themes": themes,
        "narrative": None,
        "generation": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generated_by": "stop_hook_session_summary",
            "llm_used": None,
        },
    }

    # Optional narrative via TELUS (gated)
    if _should_narrate(len(events), duration_seconds):
        narrative, llm = _try_narrate(events)
        if narrative:
            summary["narrative"] = narrative
            summary["generation"]["llm_used"] = llm

    return summary


def _compute_duration(started_iso: str, ended_iso: str) -> int:
    try:
        s = datetime.fromisoformat(started_iso.replace("Z", "+00:00"))
        e = datetime.fromisoformat(ended_iso.replace("Z", "+00:00"))
        return max(0, int((e - s).total_seconds()))
    except (ValueError, AttributeError):
        return 0


def _guess_completion_state(events: list[dict]) -> str:
    """Return shipped | abandoned | interrupted | unknown."""
    if not events:
        return "unknown"
    last = events[-1]
    if last["type"] == "Stop":
        return "shipped"
    if last["type"] == "StopFailure":
        return "abandoned"
    if last["type"] == "PreCompact":
        return "interrupted"  # session compacted but no Stop
    return "unknown"


def _extract_themes(prompts: list[dict], limit: int = 8) -> list[str]:
    """Lightweight theme extraction from user prompts."""
    text = " ".join(e.get("content", "") for e in prompts).lower()
    # Very crude: harvest hashtag-like tokens + capitalized phrases. Refine in v0.1+.
    tokens = [w.strip(".,;:!?'\"()") for w in text.split() if len(w) > 4]
    counter = Counter(t for t in tokens if t.isalpha())
    common = {
        "this", "that", "with", "have", "your", "what", "from", "would",
        "could", "should", "there", "their", "about", "which", "where",
        "these", "those", "more", "than", "then", "when", "into", "they",
        "them", "want", "need", "make", "like",
    }
    return [t for t, _ in counter.most_common(limit * 3) if t not in common][:limit]


def _should_narrate(event_count: int, duration_seconds: int) -> bool:
    if event_count < NARRATIVE_MIN_EVENTS:
        return False
    if duration_seconds < NARRATIVE_MIN_DURATION_SEC:
        return False
    if os.environ.get("LEGION_DISABLE_NARRATIVE"):
        return False
    return True


def _try_narrate(events: list[dict]) -> tuple[str | None, str | None]:
    """Optional TELUS narrative call. Returns (text, model_id) or (None, None)."""
    # V0: TELUS call deferred. Implementation hook ready.
    # Add when claude-llms ergonomics confirmed:
    #   from claude_llms import api
    #   model = os.environ.get("TELUS_LLM_MODEL", "gpt-oss:120b")
    #   resp = api.complete(model=model, prompt=_build_narrative_prompt(events),
    #                        max_tokens=NARRATIVE_MAX_OUTPUT_TOKENS)
    #   return resp.text, model
    return None, None


def _write_yaml(summary: dict[str, Any], session_id: str) -> Path:
    """Write summary to per-session YAML at ~/.claude/local/sessions/{date}/{id}/summary.yaml."""
    started = summary.get("started_at") or datetime.now(timezone.utc).isoformat()
    try:
        date = datetime.fromisoformat(started.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, AttributeError):
        date = datetime.now(timezone.utc).date().isoformat()

    out_dir = SESSIONS_ROOT / date / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "summary.yaml"
    # Atomic write: write to .tmp then replace. Avoids partial YAML on disk
    # if hook is killed mid-write (Claude Code aggressive shutdown, OOM, etc.).
    tmp_path = out_path.with_suffix(".yaml.tmp")
    tmp_path.write_text(yaml.safe_dump(summary, sort_keys=False, default_flow_style=False))
    tmp_path.replace(out_path)
    return out_path


def _upsert_db_row(db: Path, summary: dict[str, Any]) -> None:
    """Upsert into existing session_summaries table."""
    conn = sqlite3.connect(str(db))
    try:
        narrative = summary.get("narrative") or json.dumps({
            "event_count": summary["event_count"],
            "tools_used": summary.get("tools_used", {}),
            "artifacts_count": len(summary.get("artifacts", [])),
            "completion_state": summary.get("completion_state"),
        })
        entities_extracted = len(summary.get("artifacts", []))
        conn.execute(
            "INSERT OR REPLACE INTO session_summaries "
            "(session_id, summary, source, entities_extracted, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                summary["session_id"],
                narrative,
                "stop_hook",
                entities_extracted,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    payload = _read_input()
    session_id = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")
    cwd = payload.get("cwd") or os.getcwd()

    if not session_id:
        # Hook fires for every Stop; missing session_id is a hard skip, not a failure.
        return 0

    db = _resolve_db_path(cwd)
    if not db:
        # First-time-on-this-project; logging.db not yet created. Skip cleanly.
        return 0

    events = _load_session_events(db, session_id)
    summary = _compute_summary(session_id, cwd, events)

    try:
        out_path = _write_yaml(summary, session_id)
        _upsert_db_row(db, summary)
        # Quiet on success; log_event.py owns the Stop event itself.
        sys.stderr.write(f"[session_summary] wrote {out_path}\n")
    except Exception as exc:
        # Never block Claude Code on hook failure. Log and exit 0.
        sys.stderr.write(f"[session_summary] ERROR: {exc}\n")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

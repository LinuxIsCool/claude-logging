"""Recover sessions that live capture missed, from Claude Code's own transcript.

The normal path is: transcript -> hook -> plugin archive -> SQLite. Every
recovery tool in this repo (sync_backfill.py, sync_all, the rollup reconciler)
starts from the *plugin archive*. That is the right source when the hook ran and
SQLite fell behind. It is useless when the hook never ran at all: no archive
file exists, so there is nothing to re-sync, and every completeness check that
compares SQLite to the archive reports a clean bill of health.

That is exactly what happened 2026-06-30 to 2026-07-16 (manifest registered at
plugin.json instead of .claude-plugin/plugin.json): 37 sessions with a full
transcript on disk and no trace anywhere in the store, invisible to every
existing check for 20 days.

This module closes that hole by treating Claude Code's transcript under
~/.claude/projects/<slug>/ as the source of truth for "a session happened", and
reconstructing an event stream from it.

Faithfulness, deliberately bounded
----------------------------------
A transcript is richer than what the hook captures, so a naive import would make
recovered sessions look *different* from live ones. We reconstruct only the
event kinds the live path produces:

    user line (str content)          -> UserPromptSubmit
    assistant text block             -> AssistantResponse
    assistant tool_use block         -> PreToolUse
    user tool_result block           -> PostToolUse

`thinking` blocks are deliberately dropped: the live Stop hook never stores
them, and importing them would give backfilled sessions a searchable surface
that no live session has.

Every row is tagged data["_source"] = "transcript-backfill" so the corpus can
always separate reconstructed history from captured history.

Safety
------
Two properties make this safe to run against a live 89MB store:

  * Stable IDs. An event id is derived from the transcript line's own uuid, so
    re-running produces the same ids and insert_event's DELETE+INSERT makes it
    a no-op. Re-running can never duplicate rows or drift the FTS index.
  * Live sessions are never touched. Any session with a plugin archive file is
    owned by the live hook path; backfill skips it entirely rather than racing
    the writer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from lib.storage import Event, Session, StorageManager

# Transcript line types that are UI/bookkeeping, not conversation. Listing the
# ones we *keep* (rather than the ones we skip) means a new Claude Code line
# type is ignored by default instead of silently importing as garbage.
EVENT_BEARING_TYPES = {"user", "assistant"}

SOURCE_TAG = "transcript-backfill"


@dataclass
class BackfillReport:
    sessions_added: int = 0
    events_added: int = 0
    skipped_live: int = 0
    unreadable_lines: int = 0
    sessions: list[str] = field(default_factory=list)


def _event_id(session_id: str, line_uuid: str, kind: str, idx: int) -> str:
    """Deterministic id. One transcript line can yield several events (an
    assistant line with text plus two tool_use blocks), so the block index and
    kind are part of the key."""
    raw = f"{session_id}:{line_uuid}:{kind}:{idx}"
    return "evt_bf" + hashlib.sha1(raw.encode()).hexdigest()[:10]


def _text_of(blocks) -> str:
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


def events_from_transcript(path: Path, session_id: str) -> list[Event]:
    """Reconstruct the event stream for one session from its transcript."""
    events: list[Event] = []

    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                # A session killed mid-write leaves a partial final line. The
                # complete events before it are real; do not lose them.
                continue
            if not isinstance(line, dict) or line.get("type") not in EVENT_BEARING_TYPES:
                continue

            ts = line.get("timestamp") or ""
            uid = line.get("uuid") or hashlib.sha1(raw.encode()).hexdigest()[:16]
            # Subagent turns share the parent's transcript. agent_session_num
            # mirrors the live hook's convention: 0 = main thread.
            agent_num = 1 if line.get("isSidechain") else 0
            message = line.get("message") or {}
            content = message.get("content")

            if line["type"] == "user":
                if isinstance(content, str):
                    if not content.strip():
                        continue
                    events.append(
                        Event(
                            id=_event_id(session_id, uid, "prompt", 0),
                            session_id=session_id,
                            type="UserPromptSubmit",
                            ts=ts,
                            agent_session_num=agent_num,
                            data={"prompt": content, "_source": SOURCE_TAG},
                            content=content,
                        )
                    )
                elif isinstance(content, list):
                    for i, block in enumerate(content):
                        if block.get("type") != "tool_result":
                            continue
                        body = block.get("content")
                        if isinstance(body, list):
                            body = _text_of(body)
                        body = "" if body is None else str(body)
                        events.append(
                            Event(
                                id=_event_id(session_id, uid, "result", i),
                                session_id=session_id,
                                type="PostToolUse",
                                ts=ts,
                                agent_session_num=agent_num,
                                data={
                                    "tool_use_id": block.get("tool_use_id"),
                                    "_source": SOURCE_TAG,
                                },
                                content=body[:20000],
                            )
                        )

            elif line["type"] == "assistant" and isinstance(content, list):
                text = _text_of(content)
                if text:
                    events.append(
                        Event(
                            id=_event_id(session_id, uid, "assistant", 0),
                            session_id=session_id,
                            type="AssistantResponse",
                            ts=ts,
                            agent_session_num=agent_num,
                            data={
                                "response": text,
                                "model": message.get("model", ""),
                                "_source": SOURCE_TAG,
                            },
                            content=text,
                        )
                    )
                for i, block in enumerate(content):
                    if block.get("type") != "tool_use":
                        continue
                    events.append(
                        Event(
                            id=_event_id(session_id, uid, "tool", i),
                            session_id=session_id,
                            type="PreToolUse",
                            ts=ts,
                            agent_session_num=agent_num,
                            data={
                                "tool_name": block.get("name"),
                                "tool_input": block.get("input", {}),
                                "_source": SOURCE_TAG,
                            },
                            content=f"Tool: {block.get('name')}",
                            tool_name=block.get("name"),
                        )
                    )

    return events


def _dedupe(events: list[Event]) -> list[Event]:
    """Collapse events that share an id.

    Claude Code sometimes writes the same transcript line twice (observed: one
    duplicated tool_result, identical uuid, ts and content), and some lines
    carry no uuid at all, in which case the id falls back to a hash of the raw
    line so byte-identical lines also collide. Both cases are genuine
    duplicates and collapsing them is correct.

    Doing it here rather than letting insert_event's DELETE+INSERT absorb it
    keeps the reported count equal to rows actually written. A backfill report
    that overstates by even one is a report you cannot reconcile against the
    database, and reconciling counts is the whole point of this exercise.
    """
    seen: dict[str, Event] = {}
    for event in events:
        seen.setdefault(event.id, event)
    return list(seen.values())


def _session_meta(path: Path, session_id: str, events: list[Event]) -> Session:
    cwd = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(line, dict) and line.get("cwd"):
                cwd = line["cwd"]
                break

    stamps = sorted(e.ts for e in events if e.ts)
    return Session(
        id=session_id,
        started_at=stamps[0] if stamps else "",
        ended_at=stamps[-1] if stamps else None,
        cwd=cwd,
        event_count=len(events),
    )


def missing_sessions(store: StorageManager, transcript_dir: Path) -> list[str]:
    """Transcripts on disk with no session row in the store.

    This is the completeness invariant the plugin never checked: it compares
    against Claude Code's transcripts, not against the plugin's own archive.
    """
    if not transcript_dir.exists():
        return []
    known = {
        r[0] for r in store.sqlite.conn.execute("SELECT id FROM sessions")
    }
    return sorted(
        p.stem for p in transcript_dir.glob("*.jsonl") if p.stem not in known
    )


def backfill_project(
    store: StorageManager, transcript_dir: Path, dry_run: bool = False
) -> BackfillReport:
    """Import every transcript the store is missing. Idempotent."""
    report = BackfillReport()

    for session_id in missing_sessions(store, transcript_dir):
        # The live hook owns any session it has an archive for. Never race it.
        if store.jsonl.get_session_path(session_id).exists():
            report.skipped_live += 1
            continue

        path = transcript_dir / f"{session_id}.jsonl"
        events = _dedupe(events_from_transcript(path, session_id))
        if not events:
            continue

        report.sessions_added += 1
        report.events_added += len(events)
        report.sessions.append(session_id)

        if dry_run:
            continue

        # One transaction per session: a crash mid-import leaves whole sessions
        # imported or absent, never a half-imported session that then looks
        # "present" to missing_sessions() and is skipped forever.
        with store.sqlite.transaction():
            store.sqlite.insert_session(_session_meta(path, session_id, events), commit=False)
            for event in events:
                store.sqlite.insert_event(event, commit=False)

    return report

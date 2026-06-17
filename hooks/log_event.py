#!/usr/bin/env python3
"""
Main event logging hook for Claude Code.

Receives JSON event data via STDIN from Claude Code hooks,
processes the event, and stores it in JSONL format + human-readable Markdown.

Usage:
    echo '{"session_id":"...","data":{...}}' | python log_event.py -e EventType
"""

import argparse
import contextlib
import json
import os
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
# task-508 Phase 1.3: hashlib / mimetypes / b64decode moved to lib/images.py

# Cross-platform file locking
if sys.platform == "win32":

    class _NoOpFcntl:
        """No-op file locking on Windows. See README for platform notes."""

        LOCK_EX = 0
        LOCK_UN = 0

        @staticmethod
        def flock(fd, op):
            pass

    fcntl = _NoOpFcntl()
else:
    import fcntl

# Ensure lib is importable (plugin scripts aren't installed as packages)
_PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from lib import encode_project_path  # noqa: E402

# task-508 Phase 1.3 — image extraction extracted to lib/images.py
# (Spike B byte-identity verified: tests/test_spike_b_images_byte_identity.py)
from lib.images import (  # noqa: E402
    ALLOWED_IMAGE_TYPES,
    extract_images_from_prompt,
    extract_images_from_transcript,
    get_images_dir,
)

def _load_emoji_flat() -> dict[str, str]:
    """Load emoji from emoji_flat.json (canonical source). Falls back to empty dict."""
    try:
        import json as _json
        _path = Path("~/.claude/local/emoji/emoji_flat.json").expanduser()
        return _json.loads(_path.read_text())
    except Exception:
        return {}

_EMOJI_FLAT = _load_emoji_flat()


def emoji_for_event(event_type: str) -> str:
    """Resolve emoji for a hook event type from emoji_flat.json."""
    import re
    kebab = re.sub(r'(?<!^)(?=[A-Z])', '-', event_type).lower()
    return _EMOJI_FLAT.get(f"event:{kebab}", "•")

# ALLOWED_IMAGE_TYPES is now imported from lib.images (task-508 Phase 1.3)

# Health monitoring constants
HEALTH_DIR = Path.home() / ".claude" / "local" / "health"
# Max age in seconds before a heartbeat is considered stale
HEARTBEAT_MAX_AGE_SECONDS = 86400  # 24 hours


def _load_heartbeat_config() -> tuple:
    """Load heartbeat pipeline names from config, defaulting to core only.

    Users can create ~/.claude/local/health/heartbeats.json to monitor
    additional pipelines: {"pipelines": ["logging", "embedding", "custom"]}
    """
    config_path = HEALTH_DIR / "heartbeats.json"
    if config_path.exists():
        try:
            with open(config_path) as f:
                data = json.load(f)
                return tuple(data.get("pipelines", ["logging"]))
        except Exception:
            pass
    return ("logging",)


HEARTBEAT_NAMES = _load_heartbeat_config()


def write_heartbeat(name: str) -> None:
    """Write a heartbeat file to signal pipeline health.

    Each pipeline touches its heartbeat file on successful operation.
    Monitors check mtime to detect silent failures.
    """
    try:
        HEALTH_DIR.mkdir(parents=True, exist_ok=True)
        heartbeat_path = HEALTH_DIR / f"{name}-heartbeat"
        heartbeat_path.write_text(f"{datetime.now(timezone.utc).isoformat()}\n")
    except Exception:
        pass  # Never fail on heartbeat write


def check_stale_heartbeats() -> list:
    """Check all heartbeat files for staleness.

    Returns list of (name, age_hours) for stale heartbeats.
    A heartbeat is stale if its mtime exceeds HEARTBEAT_MAX_AGE_SECONDS
    relative to now, AND it has ever been written (missing = not yet active).
    """
    stale = []
    try:
        if not HEALTH_DIR.exists():
            return stale
        now = datetime.now(timezone.utc).timestamp()
        for name in HEARTBEAT_NAMES:
            hb_path = HEALTH_DIR / f"{name}-heartbeat"
            if hb_path.exists():
                age_seconds = now - hb_path.stat().st_mtime
                if age_seconds > HEARTBEAT_MAX_AGE_SECONDS:
                    age_hours = round(age_seconds / 3600, 1)
                    stale.append((name, age_hours))
    except Exception:
        pass
    return stale


def get_storage_path(cwd: str | None = None) -> Path:
    """Get the centralized storage path for logging data.

    Logs are stored at ~/.claude/local/logging/<encoded-project-path>/
    where the project path is encoded by replacing / with -

    Args:
        cwd: Working directory from hook data (preferred source)
    """
    project_dir = cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    encoded = encode_project_path(project_dir)
    return Path.home() / ".claude" / "local" / "logging" / encoded


def get_session_path(storage_path: Path, session_id: str) -> Path:
    """Get the JSONL file path for a session."""
    sessions_dir = storage_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir / f"{session_id}.jsonl"


# get_images_dir + extract_images_from_prompt now imported from lib.images
# (task-508 Phase 1.3 — Spike B verified byte-identity)


def get_agent_session_num(session_path: Path, source: str | None) -> int:
    """
    Calculate agent_session_num from JSONL content.

    This uses the "stateless state tracking" pattern - we derive the
    count from the data itself rather than maintaining a separate counter.
    Context resets (compact/clear) increment the session number.
    """
    if not session_path.exists():
        return 1 if source in ("compact", "clear") else 0

    try:
        count = 0
        with open(session_path) as f:
            for line in f:
                if line.strip():
                    try:
                        evt = json.loads(line)
                        evt_data = evt.get("data", {})
                        if isinstance(evt_data, dict) and evt_data.get("source") in ("compact", "clear"):
                            count += 1
                    except json.JSONDecodeError:
                        continue

        if source in ("compact", "clear"):
            count += 1

        return count
    except Exception:
        return 0


def append_events(session_path: Path, events: list) -> None:
    """
    Append multiple events to session JSONL file atomically with file locking.

    Writing multiple events in a single file operation ensures they're captured
    together without race conditions (learned from old logging system).
    """
    with open(session_path, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            for event in events:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def append_event(session_path: Path, event: dict) -> None:
    """Append single event (convenience wrapper)."""
    append_events(session_path, [event])


def extract_content(event_type: str, data: dict) -> str | None:
    """Extract human-readable content from event data."""
    if event_type == "UserPromptSubmit":
        prompt = data.get("prompt", "")
        # Handle content blocks (prompt is already text after extraction)
        # If images were extracted, a summary is added separately
        if isinstance(prompt, str):
            return prompt
        elif isinstance(prompt, list):
            # Extract text from content blocks if not yet processed
            texts = []
            for block in prompt:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif isinstance(block, str):
                    texts.append(block)
            return "\n".join(texts)
        return str(prompt)

    elif event_type in ("AssistantResponse", "assistant"):
        return data.get("response", data.get("content", ""))

    elif event_type == "PreToolUse":
        tool_name = data.get("tool_name", "Unknown")
        tool_input = data.get("tool_input", {})

        # Format based on tool type
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            desc = tool_input.get("description", "")
            return f"Running: {cmd}" + (f" ({desc})" if desc else "")
        elif tool_name == "Read":
            return f"Reading file: {tool_input.get('file_path', '')}"
        elif tool_name == "Write":
            return f"Writing file: {tool_input.get('file_path', '')}"
        elif tool_name == "Edit":
            return f"Editing file: {tool_input.get('file_path', '')}"
        elif tool_name == "Glob":
            return f"Finding files: {tool_input.get('pattern', '')}"
        elif tool_name == "Grep":
            return f"Searching for: {tool_input.get('pattern', '')}"
        elif tool_name == "Task":
            # ZERO TRUNCATION: full prompt
            return f"Spawning agent: {tool_input.get('description', tool_input.get('prompt', ''))}"
        else:
            # ZERO TRUNCATION: full input
            return f"{tool_name}: {str(tool_input)}"

    elif event_type == "PostToolUse":
        tool_name = data.get("tool_name", "Unknown")
        response = data.get("tool_response", {})

        if tool_name == "Bash":
            stdout = response.get("stdout", "") if isinstance(response, dict) else str(response)
            if stdout:
                # ZERO TRUNCATION: full stdout. Display layer ellipsizes.
                lines = stdout.strip().split("\n")
                return f"Output ({len(lines)} lines): {stdout}"
            return "Command completed (no output)"
        elif tool_name == "Read":
            return "File read successfully"
        elif tool_name == "Glob":
            if isinstance(response, dict):
                count = response.get("numFiles", 0)
                return f"Found {count} files"
            return "Glob completed"
        elif tool_name == "Grep":
            return "Search completed"
        else:
            return f"{tool_name} completed"

    elif event_type == "SubagentStop":
        agent_type = data.get("agent_type", "")
        return f"Agent '{agent_type}' finished"

    elif event_type == "SessionStart":
        source = data.get("source", "startup")
        model = data.get("model", "unknown")
        return f"Session started ({source}) - Model: {model}"

    elif event_type == "SessionEnd":
        return "Session ended"

    elif event_type == "Stop":
        return "Claude finished responding"

    elif event_type == "PreCompact":
        return "Context compaction starting"

    elif event_type == "PostCompact":
        summary = data.get("summary", "")
        stats = data.get("stats", {})
        if summary:
            return f"Context compacted: {summary}"
        elif stats:
            return f"Context compacted: {json.dumps(stats)}"
        return "Context compaction completed"

    elif event_type == "Notification":
        return data.get("message", "Notification")

    return None


def quote(text: str) -> str:
    """Convert text to markdown blockquote."""
    return "\n".join(f"> {line}" for line in text.split("\n"))


def tool_preview(data: dict) -> str:
    """Extract preview string from tool input."""
    inp = data.get("tool_input", {})
    if isinstance(inp, str):
        return inp
    for key in ("file_path", "pattern", "query", "command", "prompt"):
        if key in inp:
            val = str(inp[key])
            return val[:80] + "..." if len(val) > 80 else val
    return ""


def get_response(transcript_path: str) -> str:
    """Extract last assistant response from Claude's transcript."""
    try:
        for line in reversed(Path(transcript_path).read_text(encoding="utf-8").strip().split("\n")):
            if line.strip():
                entry = json.loads(line)
                if entry.get("type") == "assistant":
                    for block in entry.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            return block.get("text", "")
    except Exception:
        pass
    return ""


# extract_images_from_transcript now imported from lib.images
# (task-508 Phase 1.3 — Spike B verified byte-identity, see lib/images.py)


def update_session_with_images(session_path: Path, image_refs_by_msg: dict[int, list[dict[str, Any]]]) -> None:
    """
    Add image references to UserPromptSubmit events in the session file.

    This correlates user messages from Claude's transcript with our logged
    events by sequence position. The 1st user message maps to the 1st
    UserPromptSubmit, etc.

    Uses r+ mode with lock held for the entire read-modify-write cycle
    to prevent TOCTOU races with concurrent append_event calls.

    Args:
        session_path: Path to session JSONL file
        image_refs_by_msg: Mapping of user message index to image references
    """
    if not image_refs_by_msg or not session_path.exists():
        return

    try:
        with open(session_path, "r+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                # Read all events while holding the lock
                content = f.read()
                events = []
                user_prompt_indices = []

                for line in content.strip().split("\n"):
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    events.append(event)
                    if event.get("type") == "UserPromptSubmit":
                        user_prompt_indices.append(len(events) - 1)

                # Match UserPromptSubmit events to transcript user messages
                updated = False
                for msg_idx, image_refs in image_refs_by_msg.items():
                    if msg_idx < len(user_prompt_indices):
                        event_idx = user_prompt_indices[msg_idx]
                        if "images" not in events[event_idx]:
                            events[event_idx]["images"] = image_refs
                            updated = True

                # Rewrite file while still holding the lock
                if updated:
                    f.seek(0)
                    f.truncate()
                    for event in events:
                        f.write(json.dumps(event, ensure_ascii=False) + "\n")
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    except Exception as e:
        log_error(e, "UpdateSessionImages")


def get_subagent_info(transcript_path: str) -> dict[str, Any]:
    """Extract model, tools, and response from subagent transcript."""
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8").strip().split("\n")
        model, tools, responses = "", [], []

        for line in lines:
            if not line.strip():
                continue
            data = json.loads(line)

            # Get model from first entry
            if not model:
                m = data.get("message", {}).get("model", "")
                if "opus" in m:
                    model = "opus"
                elif "sonnet" in m:
                    model = "sonnet"
                elif "haiku" in m:
                    model = "haiku"

            # Extract tools and text from all entries
            for block in data.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input", {})
                    preview = ""
                    for k in ("file_path", "pattern", "query", "command"):
                        if k in inp:
                            preview = str(inp[k])[:60]
                            break
                    tools.append(f"- {name} `{preview}`" if preview else f"- {name}")
                elif block.get("type") == "text":
                    text = block.get("text", "").strip()
                    if text:
                        responses.append(text)

        return {"model": model, "tools": tools, "response": "\n\n".join(responses)}
    except Exception:
        return {"model": "", "tools": [], "response": ""}


def extract_subagent_transcript(transcript_path: str) -> dict[str, Any]:
    """Extract full content and metadata from a subagent transcript JSONL file.

    Reads the subagent's native transcript and extracts:
    - All assistant text responses (concatenated for FTS/embedding indexing)
    - The initial user prompt (what the subagent was asked to do)
    - Model, tools used, token counts, turn count, timestamps

    Returns a dict with keys: content, first_prompt, model, tools, tool_names,
    turn_count, token_usage, timestamps. On any error, returns safe defaults.
    """
    empty = {
        "content": "",
        "first_prompt": "",
        "model": "",
        "tools": [],
        "tool_names": [],
        "turn_count": 0,
        "token_usage": {},
        "timestamps": {},
    }
    try:
        path = Path(transcript_path)
        if not path.exists():
            return empty

        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return empty

        model = ""
        tools = []
        tool_names = []
        responses = []
        first_prompt = ""
        turn_count = 0
        first_ts = ""
        last_ts = ""
        token_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

        for line in text.split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # Skip corrupt lines, preserve valid ones

            # Track timestamps
            ts = entry.get("timestamp", "")
            if ts and not first_ts:
                first_ts = ts
            if ts:
                last_ts = ts

            entry_type = entry.get("type", "")
            message = entry.get("message", {})

            # First user entry → first_prompt
            if entry_type == "user" and not first_prompt:
                msg_content = message.get("content", "")
                if isinstance(msg_content, str):
                    first_prompt = msg_content
                elif isinstance(msg_content, list):
                    text_parts = [
                        b.get("text", "") for b in msg_content if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    first_prompt = "\n".join(text_parts)

            # Assistant entries → content, tools, model, tokens
            if entry_type == "assistant":
                turn_count += 1

                # Model detection (first assistant entry)
                if not model:
                    m = message.get("model", "")
                    if "opus" in m:
                        model = "opus"
                    elif "sonnet" in m:
                        model = "sonnet"
                    elif "haiku" in m:
                        model = "haiku"

                # Token usage aggregation
                usage = message.get("usage", {})
                for key in token_usage:
                    token_usage[key] += usage.get(key, 0)

                # Content blocks → text and tools
                for block in message.get("content", []):
                    if not isinstance(block, dict):
                        continue

                    if block.get("type") == "text":
                        t = block.get("text", "").strip()
                        if t:
                            responses.append(t)

                    elif block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        tool_names.append(name)
                        inp = block.get("input", {})
                        preview = ""
                        for k in ("file_path", "pattern", "query", "command"):
                            if k in inp:
                                preview = str(inp[k])[:60]
                                break
                        tools.append(f"- {name} `{preview}`" if preview else f"- {name}")

        return {
            "content": "\n\n".join(responses),
            "first_prompt": first_prompt,
            "model": model,
            "tools": tools,
            "tool_names": tool_names,
            "turn_count": turn_count,
            "token_usage": token_usage,
            "timestamps": {"start": first_ts, "end": last_ts},
        }
    except Exception:
        return empty


def _format_token_meta(info: dict[str, Any]) -> str:
    """Build the '— N turns, X.XK tokens' suffix for subagent labels."""
    parts = []
    if info.get("turn_count"):
        parts.append(f"{info['turn_count']} turns")
    tok = info.get("token_usage", {})
    total = tok.get("input_tokens", 0) + tok.get("output_tokens", 0)
    if total:
        parts.append(f"{total / 1000:.1f}K tokens" if total >= 1000 else f"{total} tokens")
    return f" — {', '.join(parts)}" if parts else ""


def render_subagent_md(ts: str, agent_id: str, info: dict[str, Any]) -> list[str]:
    """Render a subagent block as collapsible markdown lines.

    Used by both in-exchange and out-of-exchange SubagentStop rendering paths.
    Never truncates content — uses nested collapsible blocks for long sections.
    """
    model_tag = f" ({info['model']})" if info.get("model") else ""
    meta_str = _format_token_meta(info)
    sa_label = f"`{ts}` 🔵 Subagent {agent_id}{model_tag}{meta_str}"

    if not (info.get("tools") or info.get("response")):
        return [sa_label]

    block = ["<details>", f"<summary>{sa_label}</summary>", ""]
    if info.get("first_prompt"):
        block.extend(
            [
                "<details>",
                "<summary><strong>Prompt</strong></summary>",
                "",
                quote(info["first_prompt"]),
                "",
                "</details>",
                "",
            ]
        )
    if info.get("tools"):
        block.append(f"**Tools:** {len(info['tools'])}")
        block.extend(info["tools"])
        block.append("")
    if info.get("response"):
        block.extend(["**Response:**", quote(info["response"]), ""])
    block.extend(["</details>", ""])
    return block


def generate_markdown(jsonl_path: Path, md_path: Path, session_id: str) -> None:
    """Generate human-readable markdown report from JSONL source."""
    try:
        events = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").strip().split("\n") if line]
    except Exception:
        return

    if not events:
        return

    # Get agent session from first event
    agent_session = events[0].get("agent_session_num", 0)

    # Build session label
    session_label = f"{session_id[:8]}:{agent_session}"

    lines = [
        f"# Session {session_label}",
        f"**ID:** `{session_id}`",
        f"**Agent Session:** {agent_session} (context resets)",
        f"**Started:** {events[0]['ts'][:19].replace('T', ' ')}",
        "",
        "---",
        "",
    ]

    # Process events into exchanges (prompt → stop cycles)
    prompt = None
    tools: Counter = Counter()
    tool_details: list[str] = []
    subagents: list[dict] = []

    for e in events:
        t, d, ts = e["type"], e.get("data", {}), e["ts"][11:19]

        if t == "UserPromptSubmit":
            # Start new exchange
            prompt = (ts, d.get("prompt", ""))
            tools = Counter()
            tool_details = []
            subagents = []

        elif t == "PreToolUse" and prompt:
            name, preview = d.get("tool_name", "?"), tool_preview(d)
            # Skip AskUserQuestion pre - we render Q&A in PostToolUse
            if name != "AskUserQuestion":
                tool_details.append(f"- {name} `{preview}`" if preview else f"- {name}")

        elif t == "PostToolUse" and prompt:
            tool_name = d.get("tool_name", "?")
            tools[tool_name] += 1

            # Render AskUserQuestion Q&A inline
            if tool_name == "AskUserQuestion":
                tool_response = d.get("tool_response", {})
                answers = tool_response.get("answers", {})
                questions = tool_response.get("questions", [])

                for q_obj in questions:
                    question = q_obj.get("question", "")
                    header = q_obj.get("header", "")
                    answer = answers.get(question, "")

                    if question and answer:
                        label = f"**{header}:** " if header else ""
                        tool_details.append(f"- 💬 {label}{question}")
                        for line in answer.split("\n"):
                            tool_details.append(f"  > {line}")

        elif t == "SubagentStop" and prompt is not None:
            # Collect subagent info — prefer enriched transcript_summary, fall back to file read
            agent_id = d.get("agent_id", "?")
            summary = d.get("transcript_summary")
            if summary:
                info = {
                    "model": summary.get("model", ""),
                    "tools": summary.get("tools", []),
                    "response": e.get("content", ""),
                    "turn_count": summary.get("turn_count", 0),
                    "token_usage": summary.get("token_usage", {}),
                    "first_prompt": summary.get("first_prompt", ""),
                }
            else:
                transcript = d.get("agent_transcript_path", "")
                info = get_subagent_info(transcript) if transcript else {}
            subagents.append({"ts": ts, "id": agent_id, **info})

        elif t == "AssistantResponse":
            # Complete the exchange
            if prompt:
                ts_prompt, text = prompt
                lines.extend(["", "---", "", f"`{ts_prompt}` 🍄 User", quote(text), ""])

                if tools:
                    summary = ", ".join(f"{n} ({c})" for n, c in tools.most_common())
                    lines.extend(
                        [
                            "<details>",
                            f"<summary>📦 {sum(tools.values())} tools: {summary}</summary>",
                            "",
                            *tool_details,
                            "",
                            "</details>",
                            "",
                        ]
                    )

                if subagents:
                    for sa in subagents:
                        lines.extend(render_subagent_md(sa["ts"], sa.get("id", "?"), sa))

                prompt = None

            response = d.get("response", "")
            lines.extend(
                [
                    "<details>",
                    f"<summary>`{ts}` 🌲 Claude</summary>",
                    "",
                    quote(response),
                    "",
                    "</details>",
                    "",
                ]
            )

        elif t == "SubagentStop" and prompt is None:
            # Subagent outside of an exchange — use enriched data if available
            agent_id = d.get("agent_id", "?")
            summary = d.get("transcript_summary")
            if summary:
                info = {
                    "model": summary.get("model", ""),
                    "tools": summary.get("tools", []),
                    "response": e.get("content", ""),
                    "turn_count": summary.get("turn_count", 0),
                    "token_usage": summary.get("token_usage", {}),
                    "first_prompt": summary.get("first_prompt", ""),
                }
            else:
                transcript = d.get("agent_transcript_path", "")
                info = get_subagent_info(transcript) if transcript else {}

            lines.extend(render_subagent_md(ts, agent_id, info))

        elif t in ("SessionStart", "SessionEnd", "Notification", "PreCompact", "PostCompact"):
            info = d.get("source") or d.get("message") or d.get("summary") or ""
            emoji = emoji_for_event(t)
            lines.append(f"`{ts}` {emoji} {t} {info}".rstrip())

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def process_event(event_type: str, stdin_data: dict) -> dict:
    """Process a hook event and return the structured event."""
    # Get cwd from hook data - this is where Claude Code is running
    cwd = stdin_data.get("cwd") or stdin_data.get("data", {}).get("cwd")
    storage_path = get_storage_path(cwd)
    session_id = stdin_data.get("session_id", "unknown")
    data = stdin_data.get("data", stdin_data)

    # Extract source for session tracking
    source = None
    if isinstance(data, dict):
        source = data.get("source")

    session_path = get_session_path(storage_path, session_id)
    md_path = session_path.with_suffix(".md")

    # Build event
    ts = datetime.now(timezone.utc)
    event_id = f"evt_{uuid.uuid4().hex[:12]}"
    agent_session_num = get_agent_session_num(session_path, source)
    event = {
        "id": event_id,
        "type": event_type,
        "ts": ts.isoformat(),
        "session_id": session_id,
        "agent_session_num": agent_session_num,
        "data": data,
    }

    # task-508 Phase 1.4 — additive capture-time enrichment
    # Populate persona / agent_id / tool_name / tool_input_hash when
    # deterministic. Fields are top-level on the event dict so sync_session()
    # picks them up via _EVENT_FIELDS filter into the Event dataclass.
    persona = os.environ.get("PERSONA_SLUG")
    if persona:
        event["persona"] = persona
    agent_id_env = os.environ.get("CLAUDE_MATRIX_AGENT_ID")
    if agent_id_env:
        event["agent_id"] = agent_id_env
    if event_type in ("PreToolUse", "PostToolUse", "PostToolUseFailure") and isinstance(data, dict):
        tool_name = data.get("tool_name")
        if tool_name:
            event["tool_name"] = tool_name
        tool_input = data.get("tool_input")
        if tool_input is not None:
            try:
                import hashlib as _hashlib
                input_json = json.dumps(tool_input, sort_keys=True, default=str)
                event["tool_input_hash"] = _hashlib.sha256(input_json.encode()).hexdigest()[:16]
            except Exception as e:
                log_error(e, "ToolInputHash")

    # Handle UserPromptSubmit: extract images if prompt contains content blocks
    if event_type == "UserPromptSubmit" and isinstance(data, dict):
        prompt = data.get("prompt")
        if isinstance(prompt, list):
            # Extract images and get combined text
            text_content, image_refs = extract_images_from_prompt(
                prompt, storage_path, session_id, event_id, log_error=log_error
            )
            # Update data with extracted text (for searchability and display)
            data["prompt"] = text_content
            # Add image references if any were extracted
            if image_refs:
                event["images"] = image_refs

    # Add searchable content
    content = extract_content(event_type, data)
    if content:
        event["content"] = content

    # task-4155 — tag provenance at capture (human signal vs machine noise)
    if event_type == "UserPromptSubmit":
        from lib.provenance import classify as _classify_provenance
        event["provenance"] = _classify_provenance(content)

    # For SubagentStop: extract full transcript content for searchability
    # The transcript file already exists on disk when this hook fires.
    # We extract all assistant text for FTS/embeddings and structured metadata for rendering.
    if event_type == "SubagentStop" and isinstance(data, dict):
        transcript_path = data.get("agent_transcript_path", "")
        if transcript_path and Path(transcript_path).exists():
            try:
                transcript_info = extract_subagent_transcript(transcript_path)

                # Override content with full searchable text
                searchable_parts = []
                agent_type = data.get("agent_type", "")
                if agent_type:
                    searchable_parts.append(f"Subagent: {agent_type}")
                if transcript_info.get("first_prompt"):
                    searchable_parts.append(transcript_info["first_prompt"])
                if transcript_info.get("content"):
                    searchable_parts.append(transcript_info["content"])
                if searchable_parts:
                    event["content"] = "\n\n".join(searchable_parts)
                    content = event["content"]  # Update local var for embedding pipeline

                # Store structured metadata in data dict (no schema change needed)
                data["transcript_summary"] = {
                    "model": transcript_info.get("model", ""),
                    "tool_names": transcript_info.get("tool_names", []),
                    "tools": transcript_info.get("tools", []),
                    "turn_count": transcript_info.get("turn_count", 0),
                    "token_usage": transcript_info.get("token_usage", {}),
                    "first_prompt": transcript_info.get("first_prompt", ""),
                    "timestamps": transcript_info.get("timestamps", {}),
                }
            except Exception as e:
                log_error(e, "SubagentTranscriptExtraction")

    # For Stop events: capture assistant response and write BOTH atomically
    # This is the key insight from the old logging system that works consistently:
    # - Write both events in a single file operation
    # - No retry delays needed (transcript is already written by Claude Code)
    # - No deduplication needed (simpler = more reliable)
    if event_type == "Stop" and isinstance(data, dict) and data.get("transcript_path"):
        transcript_path = data["transcript_path"]
        events_to_write = [event]

        # Capture response immediately - transcript should already be written
        response = get_response(transcript_path)
        if response:
            assistant_event = {
                "id": f"evt_{uuid.uuid4().hex[:12]}",
                "type": "AssistantResponse",
                "ts": ts.isoformat(),
                "session_id": session_id,
                "agent_session_num": agent_session_num,
                "data": {"response": response},
                "content": response,
                # task-508 Phase 1.4 — inherit persona/agent_id from parent event
                **({"persona": event["persona"]} if "persona" in event else {}),
                **({"agent_id": event["agent_id"]} if "agent_id" in event else {}),
            }
            events_to_write.append(assistant_event)

        # Write both events atomically in single file operation
        append_events(session_path, events_to_write)

        # Extract images from transcript and update prior UserPromptSubmit events
        # Claude Code doesn't pass image data to hooks, so we extract from the
        # transcript after the conversation turn is complete
        try:
            image_refs_by_msg = extract_images_from_transcript(
                transcript_path, storage_path, session_id, log_error=log_error
            )
            if image_refs_by_msg:
                update_session_with_images(session_path, image_refs_by_msg)
        except Exception as e:
            log_error(e, "ImageExtractionFromTranscript")
    else:
        # Non-Stop events: write normally
        append_event(session_path, event)

    # Regenerate markdown on key events
    if event_type in (
        "SessionStart",
        "UserPromptSubmit",
        "Stop",
        "SessionEnd",
        "SubagentStop",
        "Notification",
        "PostCompact",
    ):
        with contextlib.suppress(Exception):
            generate_markdown(session_path, md_path, session_id)

    # Incremental SQLite sync on turn boundaries (keeps FTS5 index fresh)
    # - Stop/SubagentStop/PostCompact: sync current session only (fast, mid-session)
    # - SessionEnd: sync ALL sessions to catch any that fell through the cracks
    # - SessionStart: sync ALL sessions as a startup catch-up (prevents drift)
    if event_type in ("Stop", "SubagentStop", "PostCompact"):
        try:
            from lib.storage import StorageManager

            sm = StorageManager(storage_path)
            try:
                sm.sync_session(session_id)
            finally:
                sm.close()
            write_heartbeat("logging")
        except Exception as e:
            log_error(e, f"SQLiteSync:{event_type}")
    elif event_type in ("SessionStart", "SessionEnd"):
        try:
            from lib.storage import StorageManager

            sm = StorageManager(storage_path)
            try:
                sm.sync_all()
            finally:
                sm.close()
            write_heartbeat("logging")
        except Exception as e:
            log_error(e, f"SQLiteSyncAll:{event_type}")

    # PostCompact: capture session summary and extract entities
    if event_type == "PostCompact" and isinstance(data, dict):
        summary = data.get("summary", "")
        if summary:
            try:
                from lib.session_capture import process_postcompact_summary

                db_path = storage_path / "db" / "logging.db"
                process_postcompact_summary(db_path, session_id, summary)
            except Exception as e:
                log_error(e, "PostCompactCapture")

    # Incremental embedding on turn boundaries (keeps semantic index fresh)
    HIGH_VALUE_TYPES = ("UserPromptSubmit", "AssistantResponse", "Stop", "SubagentStop")
    if event_type in HIGH_VALUE_TYPES and content:
        try:
            emb_db = storage_path / "db" / "embeddings.db"
            if emb_db.exists():
                from lib.embeddings import EmbeddingService, EmbeddingStorage

                svc = EmbeddingService()
                if svc.is_available:
                    store = EmbeddingStorage(emb_db, dimension=svc.dimension)
                    try:
                        embedding = svc.encode([content])[0]
                        store.store(
                            event["id"],
                            embedding,
                            {
                                "session_id": session_id,
                                "event_type": event_type,
                                "content": content,
                                "timestamp": event["ts"],
                            },
                        )
                        # Heartbeat: embedding pipeline is healthy
                        write_heartbeat("embedding")
                    finally:
                        store.close()
        except Exception as e:
            log_error(e, f"Embedding:{event_type}")

    return event


def log_error(error: Exception, event_type: str) -> None:
    """Log error to file (never to stdout/stderr)."""
    try:
        storage_path = get_storage_path()
        error_log = storage_path / "errors.log"
        error_log.parent.mkdir(parents=True, exist_ok=True)

        with open(error_log, "a") as f:
            timestamp = datetime.now(timezone.utc).isoformat()
            f.write(f"{timestamp} [{event_type}] ERROR: {error}\n")
    except Exception:
        pass  # Silently fail - never block Claude


def main():
    """Entry point for hook execution."""
    parser = argparse.ArgumentParser(description="Log Claude Code events")
    parser.add_argument("-e", "--event", required=True, help="Event type")
    args = parser.parse_args()

    try:
        # Read event data from STDIN
        stdin_data = json.load(sys.stdin)

        # Process and store the event
        process_event(args.event, stdin_data)

        # On SessionEnd: check for stale heartbeats and warn
        if args.event == "SessionEnd":
            stale = check_stale_heartbeats()
            if stale:
                warnings = ", ".join(f"{name} ({age_h}h stale)" for name, age_h in stale)
                # Output JSON hook response with warning
                result = {"systemMessage": f"[health] Stale pipelines: {warnings}"}
                print(json.dumps(result))

    except Exception as e:
        # Silent failure - log to file but never crash
        log_error(e, args.event)

        # Always exit successfully to not block Claude
        sys.exit(0)


if __name__ == "__main__":
    main()

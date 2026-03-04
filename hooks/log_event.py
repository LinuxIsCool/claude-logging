#!/usr/bin/env python3
"""
Main event logging hook for Claude Code.

Receives JSON event data via STDIN from Claude Code hooks,
processes the event, and stores it in JSONL format + human-readable Markdown.

Usage:
    echo '{"session_id":"...","data":{...}}' | python log_event.py -e EventType
"""

import json
import sys
import argparse
import os
import fcntl
import hashlib
import mimetypes
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import uuid
from base64 import b64decode
from typing import Optional, List, Dict, Any, Tuple

# Emojis for visual distinction in markdown
EMOJIS = {
    "SessionStart": "💫",
    "SessionEnd": "⭐",
    "UserPromptSubmit": "🍄",
    "PreToolUse": "🔨",
    "PostToolUse": "🏰",
    "Notification": "🟡",
    "PreCompact": "♻️",
    "Stop": "🟢",
    "SubagentStop": "🔵",
    "AssistantResponse": "🌲",
}


def get_storage_path(cwd: Optional[str] = None) -> Path:
    """Get the storage path for logging data.

    Args:
        cwd: Working directory from hook data (preferred source)
    """
    # Check for explicit setting
    storage_path = os.environ.get("LOGGING_STORAGE_PATH")
    if storage_path:
        return Path(storage_path)

    # Use cwd from hook data first, then env var, then os.getcwd()
    project_dir = cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return Path(project_dir) / ".claude" / "local" / "logging"


def get_session_path(storage_path: Path, session_id: str) -> Path:
    """Get the JSONL file path for a session."""
    sessions_dir = storage_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir / f"{session_id}.jsonl"


def get_images_dir(storage_path: Path, session_id: str) -> Path:
    """Get the images directory for a session."""
    images_dir = storage_path / "images" / session_id
    images_dir.mkdir(parents=True, exist_ok=True)
    return images_dir


def extract_images_from_prompt(
    prompt: Any,
    storage_path: Path,
    session_id: str,
    event_id: str
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Extract images from prompt content blocks and save to files.

    When users paste images into Claude Code, the prompt becomes an array
    of content blocks (text + image) rather than a simple string.

    Args:
        prompt: The prompt field - either a string or list of content blocks
        storage_path: Base logging storage directory
        session_id: Current session ID
        event_id: Current event ID for filename uniqueness

    Returns:
        Tuple of (combined_text, image_references)
        - combined_text: All text blocks concatenated
        - image_references: List of {"type", "path", "media_type", "size"} dicts
    """
    # If prompt is a simple string, return as-is with no images
    if isinstance(prompt, str):
        return prompt, []

    # If not a list, convert to string
    if not isinstance(prompt, list):
        return str(prompt), []

    text_parts = []
    image_refs = []
    images_dir = get_images_dir(storage_path, session_id)

    for idx, block in enumerate(prompt):
        if not isinstance(block, dict):
            text_parts.append(str(block))
            continue

        block_type = block.get("type", "")

        if block_type == "text":
            text_parts.append(block.get("text", ""))

        elif block_type == "image":
            source = block.get("source", {})

            # Currently Claude Code uses base64 encoding
            if source.get("type") == "base64":
                # Validate and normalize media type
                ALLOWED_IMAGE_TYPES = {
                    "image/jpeg", "image/jpg", "image/png",
                    "image/gif", "image/webp"
                }
                media_type = source.get("media_type", "image/jpeg")
                if media_type not in ALLOWED_IMAGE_TYPES:
                    media_type = "image/jpeg"  # Default to jpeg for unknown types

                data = source.get("data", "")

                if data:
                    try:
                        # Decode base64 image data
                        image_bytes = b64decode(data)

                        # Generate content hash for deduplication/identification
                        content_hash = hashlib.sha256(image_bytes).hexdigest()[:12]

                        # Determine file extension from media type
                        ext = mimetypes.guess_extension(media_type) or ".jpg"
                        if ext == ".jpe":  # mimetypes returns .jpe for jpeg
                            ext = ".jpg"

                        # Create filename: hash_eventId_index.ext
                        filename = f"{content_hash}_{event_id}_{idx}{ext}"
                        filepath = images_dir / filename

                        # Save image (skip if already exists - deduplication)
                        if not filepath.exists():
                            filepath.write_bytes(image_bytes)

                        # Add reference to list
                        image_refs.append({
                            "type": "image",
                            "path": f"images/{session_id}/{filename}",
                            "media_type": media_type,
                            "size": len(image_bytes),
                            "index": idx
                        })

                    except Exception as e:
                        # Log error but don't fail - continue processing
                        log_error(e, "ImageExtraction")

            elif source.get("type") == "url":
                # URL-based images - just store the reference
                url = source.get("url", "")
                if url:
                    image_refs.append({
                        "type": "image",
                        "url": url,
                        "media_type": source.get("media_type", "image/jpeg"),
                        "index": idx
                    })

    combined_text = "\n".join(text_parts) if text_parts else ""
    return combined_text, image_refs


def get_agent_session_num(session_path: Path, source: Optional[str]) -> int:
    """
    Calculate agent_session_num from JSONL content.

    This uses the "stateless state tracking" pattern - we derive the
    count from the data itself rather than maintaining a separate counter.
    Context resets (compact/clear) increment the session number.
    """
    if not session_path.exists():
        return 1 if source in ("compact", "clear") else 0

    try:
        content = session_path.read_text()
        count = (
            content.count('"source": "compact"') +
            content.count('"source": "clear"')
        )

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


def extract_content(event_type: str, data: dict) -> Optional[str]:
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
            return f"Spawning agent: {tool_input.get('description', tool_input.get('prompt', '')[:100])}"
        else:
            # Generic fallback
            return f"{tool_name}: {str(tool_input)[:200]}"

    elif event_type == "PostToolUse":
        tool_name = data.get("tool_name", "Unknown")
        response = data.get("tool_response", {})

        if tool_name == "Bash":
            stdout = response.get("stdout", "") if isinstance(response, dict) else str(response)
            if stdout:
                # Truncate long output
                lines = stdout.strip().split('\n')
                if len(lines) > 3:
                    return f"Output ({len(lines)} lines): {lines[0][:100]}..."
                return f"Output: {stdout[:200]}"
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
        for line in reversed(Path(transcript_path).read_text().strip().split("\n")):
            if line.strip():
                entry = json.loads(line)
                if entry.get("type") == "assistant":
                    for block in entry.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            return block.get("text", "")
    except Exception:
        pass
    return ""


def extract_images_from_transcript(
    transcript_path: str,
    storage_path: Path,
    session_id: str
) -> Dict[int, List[Dict[str, Any]]]:
    """
    Extract images from all user messages in Claude's transcript.

    Claude Code doesn't pass image data to hooks, but the transcript contains
    the full message content including images. We extract them here during
    the Stop hook when the transcript is complete.

    Args:
        transcript_path: Path to Claude's transcript JSONL file
        storage_path: Base logging storage directory
        session_id: Current session ID

    Returns:
        Dictionary mapping user message index (0-based) to list of image references.
        E.g., {0: [{"type": "image", "path": "...", ...}], 2: [...]}
    """
    image_refs_by_msg: Dict[int, List[Dict[str, Any]]] = {}

    try:
        transcript = Path(transcript_path)
        if not transcript.exists():
            return {}

        lines = transcript.read_text().strip().split("\n")
        user_msg_idx = 0

        for line in lines:
            if not line.strip():
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Only process user messages
            if entry.get("type") != "user":
                continue

            content = entry.get("message", {}).get("content", [])

            # Skip if content isn't a list (no content blocks)
            if not isinstance(content, list):
                user_msg_idx += 1
                continue

            # Look for image blocks in this user message
            images_in_msg = []
            images_dir = get_images_dir(storage_path, session_id)

            for block_idx, block in enumerate(content):
                if not isinstance(block, dict):
                    continue

                if block.get("type") != "image":
                    continue

                source = block.get("source", {})

                # Handle base64 images
                if source.get("type") == "base64":
                    media_type = source.get("media_type", "image/png")
                    data = source.get("data", "")

                    if not data:
                        continue

                    # Validate media type
                    ALLOWED_IMAGE_TYPES = {
                        "image/jpeg", "image/jpg", "image/png",
                        "image/gif", "image/webp"
                    }
                    if media_type not in ALLOWED_IMAGE_TYPES:
                        media_type = "image/png"

                    try:
                        # Decode image
                        image_bytes = b64decode(data)

                        # Generate content hash for deduplication
                        content_hash = hashlib.sha256(image_bytes).hexdigest()[:12]

                        # Determine file extension
                        ext = mimetypes.guess_extension(media_type) or ".png"
                        if ext == ".jpe":
                            ext = ".jpg"

                        # Filename includes user message position for correlation
                        filename = f"user{user_msg_idx}_{content_hash}_{block_idx}{ext}"
                        filepath = images_dir / filename

                        # Save image (skip if exists - deduplication)
                        if not filepath.exists():
                            filepath.write_bytes(image_bytes)

                        # Record reference
                        images_in_msg.append({
                            "type": "image",
                            "path": f"images/{session_id}/{filename}",
                            "media_type": media_type,
                            "size": len(image_bytes),
                            "index": block_idx
                        })

                    except Exception as e:
                        log_error(e, "TranscriptImageExtraction")

                # Handle URL-based images
                elif source.get("type") == "url":
                    url = source.get("url", "")
                    if url:
                        images_in_msg.append({
                            "type": "image",
                            "url": url,
                            "media_type": source.get("media_type", "image/jpeg"),
                            "index": block_idx
                        })

            # Store references for this user message if any images found
            if images_in_msg:
                image_refs_by_msg[user_msg_idx] = images_in_msg

            user_msg_idx += 1

    except Exception as e:
        log_error(e, "TranscriptImageExtraction")

    return image_refs_by_msg


def update_session_with_images(
    session_path: Path,
    image_refs_by_msg: Dict[int, List[Dict[str, Any]]]
) -> None:
    """
    Add image references to UserPromptSubmit events in the session file.

    This correlates user messages from Claude's transcript with our logged
    events by sequence position. The 1st user message maps to the 1st
    UserPromptSubmit, etc.

    Args:
        session_path: Path to session JSONL file
        image_refs_by_msg: Mapping of user message index to image references
    """
    if not image_refs_by_msg or not session_path.exists():
        return

    try:
        # Read all events
        lines = session_path.read_text().strip().split("\n")
        events = []
        user_prompt_indices = []  # Track positions of UserPromptSubmit events

        for i, line in enumerate(lines):
            if not line.strip():
                continue
            event = json.loads(line)
            events.append(event)

            if event.get("type") == "UserPromptSubmit":
                user_prompt_indices.append(len(events) - 1)

        # Match UserPromptSubmit events to transcript user messages by position
        # and add image references
        updated = False
        for msg_idx, image_refs in image_refs_by_msg.items():
            # Check if we have a corresponding UserPromptSubmit
            if msg_idx < len(user_prompt_indices):
                event_idx = user_prompt_indices[msg_idx]
                event = events[event_idx]

                # Only update if images not already set
                if "images" not in event:
                    event["images"] = image_refs
                    updated = True

        # Rewrite file if we made updates
        if updated:
            with open(session_path, "w") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    for event in events:
                        f.write(json.dumps(event, ensure_ascii=False) + "\n")
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    except Exception as e:
        log_error(e, "UpdateSessionImages")




def get_subagent_info(transcript_path: str) -> Dict[str, Any]:
    """Extract model, tools, and response from subagent transcript."""
    try:
        lines = Path(transcript_path).read_text().strip().split("\n")
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


def generate_markdown(jsonl_path: Path, md_path: Path, session_id: str) -> None:
    """Generate human-readable markdown report from JSONL source."""
    try:
        events = [
            json.loads(line) for line in jsonl_path.read_text().strip().split("\n") if line
        ]
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
    tool_details: List[str] = []
    subagents: List[Dict] = []

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
            # Collect subagent info for this exchange
            agent_id = d.get("agent_id", "?")
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
                    lines.extend([
                        "<details>",
                        f"<summary>📦 {sum(tools.values())} tools: {summary}</summary>",
                        "",
                        *tool_details,
                        "",
                        "</details>",
                        "",
                    ])

                if subagents:
                    for sa in subagents:
                        model_tag = f" ({sa['model']})" if sa.get("model") else ""
                        sa_label = f"`{sa['ts']}` 🔵 Subagent {sa['id']}{model_tag}"

                        if sa.get("tools") or sa.get("response"):
                            lines.extend(["<details>", f"<summary>{sa_label}</summary>", ""])
                            if sa.get("tools"):
                                lines.append(f"**Tools:** {len(sa['tools'])}")
                                lines.extend(sa["tools"])
                                lines.append("")
                            if sa.get("response"):
                                lines.extend(["**Response:**", quote(sa["response"][:500]), ""])
                            lines.extend(["</details>", ""])
                        else:
                            lines.append(sa_label)

                prompt = None

            response = d.get("response", "")
            lines.extend([
                "<details>",
                f"<summary>`{ts}` 🌲 Claude</summary>",
                "",
                quote(response),
                "",
                "</details>",
                "",
            ])

        elif t == "SubagentStop" and prompt is None:
            # Subagent outside of an exchange
            agent_id = d.get("agent_id", "?")
            transcript = d.get("agent_transcript_path", "")
            info = get_subagent_info(transcript) if transcript else {}
            model_tag = f" ({info['model']})" if info.get("model") else ""
            sa_label = f"`{ts}` 🔵 Subagent {agent_id}{model_tag}"

            if info.get("tools") or info.get("response"):
                lines.extend(["<details>", f"<summary>{sa_label}</summary>", ""])
                if info.get("tools"):
                    lines.append(f"**Tools:** {len(info['tools'])}")
                    lines.extend(info["tools"])
                    lines.append("")
                if info.get("response"):
                    lines.extend(["**Response:**", quote(info["response"][:500]), ""])
                lines.extend(["</details>", ""])
            else:
                lines.append(sa_label)

        elif t in ("SessionStart", "SessionEnd", "Notification", "PreCompact"):
            info = d.get("source") or d.get("message") or ""
            emoji = EMOJIS.get(t, "•")
            lines.append(f"`{ts}` {emoji} {t} {info}".rstrip())

    md_path.write_text("\n".join(lines) + "\n")


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

    # Handle UserPromptSubmit: extract images if prompt contains content blocks
    if event_type == "UserPromptSubmit" and isinstance(data, dict):
        prompt = data.get("prompt")
        if isinstance(prompt, list):
            # Extract images and get combined text
            text_content, image_refs = extract_images_from_prompt(
                prompt, storage_path, session_id, event_id
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
            }
            events_to_write.append(assistant_event)

        # Write both events atomically in single file operation
        append_events(session_path, events_to_write)

        # Extract images from transcript and update prior UserPromptSubmit events
        # Claude Code doesn't pass image data to hooks, so we extract from the
        # transcript after the conversation turn is complete
        try:
            image_refs_by_msg = extract_images_from_transcript(
                transcript_path, storage_path, session_id
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
    ):
        try:
            generate_markdown(session_path, md_path, session_id)
        except Exception:
            pass  # Never fail on markdown generation

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

        # Silent success - don't print anything to stdout/stderr

    except Exception as e:
        # Silent failure - log to file but never crash
        log_error(e, args.event)

        # Always exit successfully to not block Claude
        sys.exit(0)


if __name__ == "__main__":
    main()

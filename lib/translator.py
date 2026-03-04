"""
Event Translator

Converts Claude Code native formats (transcript JSONL) into logging events.
Handles incremental parsing with deterministic event IDs for deduplication.
"""

import json
import hashlib
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import asdict

from .storage import Event
from .claude_types import TranscriptEntry


class EventTranslator:
    """
    Translates Claude Code transcript entries to logging events.

    Key design decisions:
    - Deterministic event IDs based on source + session + content hash
    - Separate events for tool uses (PreToolUse) and responses (AssistantResponse)
    - Skip internal messages (progress, file-history-snapshot, queue-operation)
    - Flatten subagents with parent_session_id reference
    """

    # Event types we skip during translation
    SKIP_TYPES = {"progress", "file-history-snapshot", "queue-operation"}

    def __init__(self):
        pass

    def generate_event_id(
        self,
        source: str,
        session_id: str,
        identifier: str
    ) -> str:
        """
        Generate a deterministic event ID.

        Format: evt_{source}_{hash}
        where hash = first 12 chars of sha256(session_id + identifier)

        This ensures:
        - Same content always generates same ID (deduplication)
        - No conflicts with hook-generated UUIDs (different prefix)
        """
        content = f"{session_id}:{identifier}"
        hash_val = hashlib.sha256(content.encode()).hexdigest()[:12]
        return f"evt_{source}_{hash_val}"

    def translate_transcript_line(
        self,
        line: str,
        session_id: str,
        line_number: int,
        parent_session_id: Optional[str] = None
    ) -> List[Event]:
        """
        Translate a single transcript JSONL line to logging events.

        Args:
            line: Raw JSONL line
            session_id: Session UUID
            line_number: Line number in file (for ID generation)
            parent_session_id: If this is a subagent, the parent session

        Returns:
            List of Event objects (may be empty for skipped types,
            or multiple for assistant messages with tool calls)
        """
        try:
            data = json.loads(line.strip())
        except json.JSONDecodeError:
            return []

        entry = TranscriptEntry.from_dict(data)

        # Skip types we don't need
        if entry.type in self.SKIP_TYPES:
            return []

        events = []

        if entry.type == "user" and entry.is_external_user():
            events.append(self._translate_user_message(entry, session_id, line_number))

        elif entry.type == "assistant":
            events.extend(self._translate_assistant_message(entry, session_id, line_number))

        elif entry.type == "system":
            event = self._translate_system_message(entry, session_id, line_number)
            if event:
                events.append(event)

        elif entry.type == "summary":
            # Summary messages become session metadata, not events
            # Could emit a special event if needed
            pass

        # Add parent reference for subagent events
        if parent_session_id and events:
            for event in events:
                if event.data is None:
                    event.data = {}
                event.data["parent_session_id"] = parent_session_id
                event.data["is_subagent"] = True

        return events

    def _translate_user_message(
        self,
        entry: TranscriptEntry,
        session_id: str,
        line_number: int
    ) -> Event:
        """Translate external user message to UserPromptSubmit event."""
        prompt = entry.get_text_content()

        return Event(
            id=self.generate_event_id("transcript", session_id, f"user_{line_number}"),
            session_id=session_id,
            type="UserPromptSubmit",
            ts=entry.timestamp,
            agent_session_num=0,  # Will be calculated during sync
            data={
                "prompt": prompt,
                "cwd": entry.cwd,
                "source": "transcript_import",
                "original_uuid": entry.uuid,
            },
            content=prompt,
        )

    def _translate_assistant_message(
        self,
        entry: TranscriptEntry,
        session_id: str,
        line_number: int
    ) -> List[Event]:
        """
        Translate assistant message to events.

        One assistant message may produce:
        - Multiple PreToolUse events (one per tool_use block)
        - One AssistantResponse event (if text content present)
        """
        events = []
        tool_uses = entry.get_tool_uses()

        # Create PreToolUse events for each tool
        for idx, tool in enumerate(tool_uses):
            tool_name = tool.get("name", "Unknown")
            tool_input = tool.get("input", {})
            tool_id = tool.get("id", f"tool_{idx}")

            # Format content based on tool type
            content = self._format_tool_content(tool_name, tool_input)

            events.append(Event(
                id=self.generate_event_id("transcript", session_id, f"tool_{line_number}_{idx}"),
                session_id=session_id,
                type="PreToolUse",
                ts=entry.timestamp,
                agent_session_num=0,
                data={
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "tool_use_id": tool_id,
                    "source": "transcript_import",
                },
                content=content,
            ))

        # Create AssistantResponse if there's text content
        text_content = entry.get_text_content()
        if text_content:
            events.append(Event(
                id=self.generate_event_id("transcript", session_id, f"response_{line_number}"),
                session_id=session_id,
                type="AssistantResponse",
                ts=entry.timestamp,
                agent_session_num=0,
                data={
                    "response": text_content,
                    "model": entry.model,
                    "source": "transcript_import",
                    "original_uuid": entry.uuid,
                },
                content=text_content,
            ))

        return events

    def _translate_system_message(
        self,
        entry: TranscriptEntry,
        session_id: str,
        line_number: int
    ) -> Optional[Event]:
        """Translate system message to SessionStart event."""
        # Only translate the first system message as SessionStart
        # Subsequent system messages are context updates

        return Event(
            id=self.generate_event_id("transcript", session_id, f"system_{line_number}"),
            session_id=session_id,
            type="SessionStart",
            ts=entry.timestamp,
            agent_session_num=0,
            data={
                "cwd": entry.cwd,
                "version": entry.version,
                "model": entry.model,
                "git_branch": entry.gitBranch,
                "source": "transcript_import",
            },
            content=f"Session started - Model: {entry.model or 'unknown'}",
        )

    def _format_tool_content(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Format tool input for searchable content."""
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
            return f"Spawning agent: {tool_input.get('description', str(tool_input.get('prompt', ''))[:100])}"
        elif tool_name == "WebSearch":
            return f"Web search: {tool_input.get('query', '')}"
        elif tool_name == "WebFetch":
            return f"Fetching URL: {tool_input.get('url', '')}"
        else:
            # Generic fallback
            preview = str(tool_input)[:200]
            return f"{tool_name}: {preview}"

    def translate_subagent_transcript(
        self,
        transcript_path: Path,
        parent_session_id: str,
        agent_id: str
    ) -> Tuple[List[Event], Dict[str, Any]]:
        """
        Translate an entire subagent transcript.

        Args:
            transcript_path: Path to subagent JSONL file
            parent_session_id: Parent session UUID
            agent_id: Subagent identifier

        Returns:
            Tuple of (events list, summary dict with prompt/response/tool_count)
        """
        events = []
        prompt = ""
        final_response = ""
        tool_count = 0
        model = ""

        try:
            with open(transcript_path, "r") as f:
                for line_num, line in enumerate(f):
                    if not line.strip():
                        continue

                    # Translate each line
                    line_events = self.translate_transcript_line(
                        line=line,
                        session_id=agent_id,  # Subagent has its own session
                        line_number=line_num,
                        parent_session_id=parent_session_id
                    )
                    events.extend(line_events)

                    # Extract summary info
                    for event in line_events:
                        if event.type == "UserPromptSubmit" and not prompt:
                            prompt = event.content or ""
                        elif event.type == "AssistantResponse":
                            final_response = event.content or ""
                            if event.data and event.data.get("model"):
                                model = event.data["model"]
                        elif event.type == "PreToolUse":
                            tool_count += 1

        except Exception:
            pass

        summary = {
            "prompt": prompt[:500] if prompt else "",
            "response": final_response[:500] if final_response else "",
            "tool_count": tool_count,
            "model": model,
            "event_count": len(events),
        }

        return events, summary

    def parse_transcript_file(
        self,
        transcript_path: Path,
        session_id: str,
        start_position: int = 0
    ) -> Tuple[List[Event], int]:
        """
        Parse a transcript file from a given position.

        Args:
            transcript_path: Path to JSONL file
            session_id: Session UUID
            start_position: File position to start reading from

        Returns:
            Tuple of (events list, new position)
        """
        events = []
        new_position = start_position

        try:
            with open(transcript_path, "r") as f:
                line_offset = 0

                # Count lines before start_position to get correct line numbers
                if start_position > 0:
                    while f.tell() < start_position:
                        line = f.readline()
                        if not line:
                            break
                        line_offset += 1

                # Now read from start_position forward
                while True:
                    line = f.readline()
                    if not line:
                        break

                    if line.strip():
                        line_events = self.translate_transcript_line(
                            line=line,
                            session_id=session_id,
                            line_number=line_offset
                        )
                        events.extend(line_events)
                    line_offset += 1

                new_position = f.tell()

        except Exception:
            pass

        return events, new_position

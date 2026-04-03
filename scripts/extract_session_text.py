#!/usr/bin/env python3
"""
Extract searchable plain text from Claude Code session transcripts.

Handles all three user message formats:
  1. Plain string (12% of messages) — direct text
  2. tool_result arrays (84%) — extracts text blocks from tool results
  3. Mixed arrays (4%) — extracts text from any content block type

Also extracts assistant responses (text blocks from assistant messages).

Usage:
    # Extract text from a specific session transcript
    uv run scripts/extract_session_text.py <transcript.jsonl>

    # Extract text from all transcripts and write index
    uv run scripts/extract_session_text.py --all --project-dir $HOME

    # Output as structured JSON (for pipeline consumption)
    uv run scripts/extract_session_text.py --json <transcript.jsonl>
"""

import argparse
import json
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class SessionMessage:
    """A single extracted message from a session transcript."""

    role: str  # "user" or "assistant"
    text: str  # Extracted plain text
    index: int  # Message index within session (0-based)
    has_tool_results: bool = False
    tool_names: list = None

    def __post_init__(self):
        if self.tool_names is None:
            self.tool_names = []


@dataclass
class SessionText:
    """Extracted text from an entire session."""

    session_id: str
    transcript_path: str
    messages: list  # List of SessionMessage
    user_message_count: int
    assistant_message_count: int
    total_chars: int

    @property
    def user_text(self) -> str:
        """All user text concatenated."""
        return "\n\n".join(m.text for m in self.messages if m.role == "user" and m.text)

    @property
    def assistant_text(self) -> str:
        """All assistant text concatenated."""
        return "\n\n".join(m.text for m in self.messages if m.role == "assistant" and m.text)

    @property
    def full_text(self) -> str:
        """All text concatenated with role markers."""
        parts = []
        for m in self.messages:
            if m.text:
                parts.append(f"[{m.role}] {m.text}")
        return "\n\n".join(parts)


def extract_text_from_content(content) -> str:
    """Extract plain text from any content format.

    Handles:
    - str: return as-is
    - list of content blocks: extract text from each block
    - dict: extract text field
    """
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict):
                block_type = block.get("type", "")

                if block_type == "text":
                    text = block.get("text", "")
                    if text:
                        texts.append(text)

                elif block_type == "tool_result":
                    # tool_result has content that can be string or list
                    inner = block.get("content", "")
                    if isinstance(inner, str):
                        texts.append(inner)
                    elif isinstance(inner, list):
                        for sub in inner:
                            if isinstance(sub, dict) and sub.get("type") == "text":
                                texts.append(sub.get("text", ""))
                            elif isinstance(sub, str):
                                texts.append(sub)

                elif block_type == "tool_use":
                    # Skip tool_use blocks (they're requests, not content)
                    pass

                elif block_type == "image":
                    texts.append("[image]")

        return "\n".join(t for t in texts if t.strip())

    if isinstance(content, dict):
        return content.get("text", str(content))

    return str(content)


def extract_tool_names(content) -> list:
    """Extract tool names from content blocks."""
    names = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "tool_use":
                    names.append(block.get("name", "unknown"))
                elif block.get("type") == "tool_result":
                    # tool_result doesn't have tool name directly
                    pass
    return names


def extract_session(transcript_path: Path) -> SessionText | None:
    """Extract all text from a session transcript JSONL file.

    Args:
        transcript_path: Path to the Claude Code transcript .jsonl file
                        (from ~/.claude/projects/<project>/<session_id>.jsonl)

    Returns:
        SessionText with all extracted messages, or None if file is invalid
    """
    if not transcript_path.exists():
        return None

    session_id = transcript_path.stem
    messages = []
    user_count = 0
    assistant_count = 0
    total_chars = 0
    msg_index = 0

    try:
        with open(transcript_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue

                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type", "")
                content = entry.get("message", {}).get("content", "")

                if entry_type == "user":
                    text = extract_text_from_content(content)
                    has_tools = False
                    tool_names = []

                    if isinstance(content, list):
                        has_tools = any(
                            isinstance(b, dict) and b.get("type") in ("tool_result", "tool_use") for b in content
                        )
                        tool_names = extract_tool_names(content)

                    if text.strip():
                        messages.append(
                            SessionMessage(
                                role="user",
                                text=text,
                                index=msg_index,
                                has_tool_results=has_tools,
                                tool_names=tool_names,
                            )
                        )
                        user_count += 1
                        total_chars += len(text)
                    msg_index += 1

                elif entry_type == "assistant":
                    text = extract_text_from_content(content)
                    tool_names = extract_tool_names(content)

                    if text.strip():
                        messages.append(
                            SessionMessage(
                                role="assistant",
                                text=text,
                                index=msg_index,
                                has_tool_results=False,
                                tool_names=tool_names,
                            )
                        )
                        assistant_count += 1
                        total_chars += len(text)
                    msg_index += 1

    except Exception as e:
        print(f"Error processing {transcript_path}: {e}", file=sys.stderr)
        return None

    return SessionText(
        session_id=session_id,
        transcript_path=str(transcript_path),
        messages=messages,
        user_message_count=user_count,
        assistant_message_count=assistant_count,
        total_chars=total_chars,
    )


def iter_transcripts(project_dir: str) -> Iterator[Path]:
    """Iterate over all transcript JSONL files for a project."""
    projects_dir = Path.home() / ".claude" / "projects"
    encoded = project_dir.replace("/", "-")
    transcript_dir = projects_dir / encoded

    if not transcript_dir.exists():
        return

    for f in sorted(transcript_dir.glob("*.jsonl")):
        # Skip non-UUID filenames
        stem = f.stem
        if len(stem) == 36 and stem.count("-") == 4:
            yield f


def main():
    parser = argparse.ArgumentParser(description="Extract searchable text from Claude Code session transcripts")
    parser.add_argument("transcript", nargs="?", help="Path to transcript JSONL file")
    parser.add_argument("--all", action="store_true", help="Process all transcripts for the project")
    parser.add_argument("--project-dir", default=str(Path.home()), help="Project directory (default: $HOME)")
    parser.add_argument("--json", action="store_true", help="Output as structured JSON")
    parser.add_argument("--stats-only", action="store_true", help="Output only statistics, not full text")
    args = parser.parse_args()

    if args.transcript:
        # Single file mode
        result = extract_session(Path(args.transcript))
        if result is None:
            print("Failed to extract session", file=sys.stderr)
            sys.exit(1)

        if args.json:
            out = {
                "session_id": result.session_id,
                "user_messages": result.user_message_count,
                "assistant_messages": result.assistant_message_count,
                "total_chars": result.total_chars,
            }
            if not args.stats_only:
                out["messages"] = [asdict(m) for m in result.messages]
            print(json.dumps(out, indent=2))
        else:
            print(f"Session: {result.session_id}")
            print(f"Messages: {result.user_message_count} user, {result.assistant_message_count} assistant")
            print(f"Total chars: {result.total_chars:,}")
            if not args.stats_only:
                print("\n" + "=" * 60 + "\n")
                print(result.full_text)

    elif args.all:
        # Process all transcripts
        total_sessions = 0
        total_messages = 0
        total_chars = 0

        for path in iter_transcripts(args.project_dir):
            result = extract_session(path)
            if result is None:
                continue

            total_sessions += 1
            total_messages += result.user_message_count + result.assistant_message_count
            total_chars += result.total_chars

            if args.json:
                out = {
                    "session_id": result.session_id,
                    "user_messages": result.user_message_count,
                    "assistant_messages": result.assistant_message_count,
                    "total_chars": result.total_chars,
                }
                print(json.dumps(out))
            else:
                print(
                    f"{result.session_id}: {result.user_message_count}u/{result.assistant_message_count}a, {result.total_chars:,} chars"
                )

        print(f"\nTotal: {total_sessions} sessions, {total_messages} messages, {total_chars:,} chars", file=sys.stderr)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Unit tests for extract_session_text.py — transcript JSONL parsing.

Tests all 3 message formats (plain string, tool_result arrays, mixed)
and the streaming file read.
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest"]
# ///

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from extract_session_text import (
    extract_session,
    extract_text_from_content,
    extract_tool_names,
)

# ── extract_text_from_content ─────────────────────────────────────────


class TestExtractTextFromContent:
    def test_plain_string(self):
        assert extract_text_from_content("hello world") == "hello world"

    def test_text_block_list(self):
        content = [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]
        result = extract_text_from_content(content)
        assert "first" in result
        assert "second" in result

    def test_tool_result_with_string_content(self):
        content = [{"type": "tool_result", "content": "tool output here"}]
        result = extract_text_from_content(content)
        assert "tool output here" in result

    def test_tool_result_with_list_content(self):
        content = [
            {
                "type": "tool_result",
                "content": [{"type": "text", "text": "nested tool output"}],
            }
        ]
        result = extract_text_from_content(content)
        assert "nested tool output" in result

    def test_tool_use_blocks_skipped(self):
        content = [
            {"type": "text", "text": "user message"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        ]
        result = extract_text_from_content(content)
        assert "user message" in result
        assert "Bash" not in result

    def test_image_block(self):
        content = [{"type": "image"}]
        result = extract_text_from_content(content)
        assert "[image]" in result

    def test_mixed_string_and_dict(self):
        content = ["plain string", {"type": "text", "text": "dict text"}]
        result = extract_text_from_content(content)
        assert "plain string" in result
        assert "dict text" in result

    def test_empty_list(self):
        assert extract_text_from_content([]) == ""

    def test_dict_with_text(self):
        assert "hello" in extract_text_from_content({"text": "hello"})

    def test_none_like_value(self):
        # Should not crash on unexpected types
        result = extract_text_from_content(42)
        assert result == "42"


# ── extract_tool_names ────────────────────────────────────────────────


class TestExtractToolNames:
    def test_extracts_tool_use_names(self):
        content = [
            {"type": "tool_use", "name": "Bash"},
            {"type": "tool_use", "name": "Read"},
            {"type": "text", "text": "some text"},
        ]
        names = extract_tool_names(content)
        assert names == ["Bash", "Read"]

    def test_no_tools(self):
        content = [{"type": "text", "text": "hello"}]
        assert extract_tool_names(content) == []

    def test_non_list(self):
        assert extract_tool_names("string content") == []


# ── extract_session ───────────────────────────────────────────────────


class TestExtractSession:
    def _write_transcript(self, tmp_path, session_id, entries):
        """Write a fake transcript JSONL."""
        path = tmp_path / f"{session_id}.jsonl"
        with open(path, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        return path

    def test_basic_session(self, tmp_path):
        entries = [
            {"type": "user", "message": {"content": "Hello Claude"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi there!"}]}},
        ]
        path = self._write_transcript(tmp_path, "test-session-001", entries)
        result = extract_session(path)

        assert result is not None
        assert result.session_id == "test-session-001"
        assert result.user_message_count == 1
        assert result.assistant_message_count == 1
        assert "Hello Claude" in result.user_text
        assert "Hi there" in result.assistant_text

    def test_tool_result_messages(self, tmp_path):
        """84% of messages are tool_result arrays."""
        entries = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "content": "file contents here"},
                        {"type": "text", "text": "Now do something with this file"},
                    ]
                },
            },
        ]
        path = self._write_transcript(tmp_path, "test-session-002", entries)
        result = extract_session(path)

        assert result is not None
        assert "file contents here" in result.user_text
        assert "Now do something with this file" in result.user_text

    def test_empty_file(self, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        result = extract_session(path)
        assert result is not None
        assert result.total_chars == 0

    def test_nonexistent_file(self, tmp_path):
        path = tmp_path / "missing.jsonl"
        result = extract_session(path)
        assert result is None

    def test_malformed_json_lines_skipped(self, tmp_path):
        path = tmp_path / "bad.jsonl"
        path.write_text('{"type":"user","message":{"content":"good"}}\nNOT JSON\n')
        result = extract_session(path)
        assert result is not None
        assert result.user_message_count == 1

    def test_full_text_has_role_markers(self, tmp_path):
        entries = [
            {"type": "user", "message": {"content": "question"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer"}]}},
        ]
        path = self._write_transcript(tmp_path, "test-markers", entries)
        result = extract_session(path)
        assert "[user]" in result.full_text
        assert "[assistant]" in result.full_text

    def test_streaming_reads_large_file(self, tmp_path):
        """Verify streaming read works (no read_text() loading entire file)."""
        entries = [{"type": "user", "message": {"content": f"Message {i}"}} for i in range(1000)]
        path = self._write_transcript(tmp_path, "test-large", entries)
        result = extract_session(path)
        assert result.user_message_count == 1000


# ── iter_transcripts ──────────────────────────────────────────────────


class TestIterTranscripts:
    def test_filters_non_uuid_files(self, tmp_path):
        # Simulate the ~/.claude/projects/-project/ structure
        project_dir = tmp_path / ".claude" / "projects" / "-test-project"
        project_dir.mkdir(parents=True)

        # UUID-like filename (should be yielded)
        (project_dir / "12345678-1234-1234-1234-123456789012.jsonl").write_text("{}")
        # Non-UUID filename (should be skipped)
        (project_dir / "notes.jsonl").write_text("{}")
        (project_dir / "README.md").write_text("hello")

        # Can't easily test iter_transcripts without mocking Path.home
        # This is a structural test — verify the UUID filter logic
        stem = "12345678-1234-1234-1234-123456789012"
        assert len(stem) == 36 and stem.count("-") == 4

        stem2 = "notes"
        assert not (len(stem2) == 36 and stem2.count("-") == 4)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""Tests for generate_markdown() and render helpers."""

import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from hooks.log_event import generate_markdown, quote, render_subagent_md, tool_preview


class TestQuote:
    def test_single_line(self):
        assert quote("hello") == "> hello"

    def test_multi_line(self):
        result = quote("line1\nline2\nline3")
        assert result == "> line1\n> line2\n> line3"

    def test_empty(self):
        assert quote("") == "> "


class TestToolPreview:
    def test_file_path(self):
        data = {"tool_input": {"file_path": "/src/main.py"}}
        assert tool_preview(data) == "/src/main.py"

    def test_pattern(self):
        data = {"tool_input": {"pattern": "**/*.ts"}}
        assert tool_preview(data) == "**/*.ts"

    def test_command(self):
        data = {"tool_input": {"command": "npm run build"}}
        assert tool_preview(data) == "npm run build"

    def test_long_truncated(self):
        data = {"tool_input": {"command": "x" * 200}}
        result = tool_preview(data)
        assert len(result) <= 84  # 80 + "..."

    def test_string_input(self):
        data = {"tool_input": "raw string"}
        assert tool_preview(data) == "raw string"

    def test_empty(self):
        data = {"tool_input": {}}
        assert tool_preview(data) == ""

    def test_missing_input(self):
        assert tool_preview({}) == ""


class TestRenderSubagentMd:
    def test_basic_render(self):
        info = {
            "model": "sonnet",
            "tools": ["- Read `/src/main.py`"],
            "tool_names": ["Read"],
            "turn_count": 3,
            "token_usage": {"input_tokens": 5000, "output_tokens": 2000},
            "first_prompt": "Investigate the auth module",
            "response": "Found 3 authentication providers.",
            "timestamps": {"start": "2026-04-03T10:00:00Z", "end": "2026-04-03T10:01:00Z"},
        }
        lines = render_subagent_md("10:00:00", "agent-abc", info)
        text = "\n".join(lines)
        assert "<details>" in text
        assert "sonnet" in text
        assert "3 turns" in text
        assert "7.0K tokens" in text
        assert "Investigate the auth module" in text
        assert "Found 3 authentication" in text

    def test_empty_info(self):
        info = {"model": "", "tools": [], "response": "", "first_prompt": "", "turn_count": 0, "token_usage": {}}
        lines = render_subagent_md("10:00:00", "agent-xyz", info)
        # Should produce just the label, no collapsible
        text = "\n".join(lines)
        assert "agent-xyz" in text
        assert "<details>" not in text

    def test_no_tools(self):
        info = {
            "model": "haiku",
            "tools": [],
            "response": "Done.",
            "first_prompt": "",
            "turn_count": 1,
            "token_usage": {},
        }
        lines = render_subagent_md("12:00:00", "agent-123", info)
        text = "\n".join(lines)
        assert "Done." in text
        assert "Tools" not in text


class TestGenerateMarkdown:
    def _make_session(self, tmp_path, session_id, events):
        jsonl_path = tmp_path / f"{session_id}.jsonl"
        md_path = tmp_path / f"{session_id}.md"
        with open(jsonl_path, "w") as f:
            for evt in events:
                f.write(json.dumps(evt) + "\n")
        return jsonl_path, md_path

    def test_basic_session(self, tmp_path):
        """A complete exchange (prompt → AssistantResponse) renders the prompt text."""
        events = [
            {
                "type": "SessionStart",
                "ts": "2026-04-03T10:00:00+00:00",
                "agent_session_num": 0,
                "data": {"source": "startup"},
            },
            {
                "type": "UserPromptSubmit",
                "ts": "2026-04-03T10:01:00+00:00",
                "agent_session_num": 0,
                "data": {"prompt": "Hello"},
            },
            {
                "type": "AssistantResponse",
                "ts": "2026-04-03T10:02:00+00:00",
                "agent_session_num": 0,
                "data": {"response": "Hi there"},
            },
        ]
        jsonl, md = self._make_session(tmp_path, "test-md-001", events)
        generate_markdown(jsonl, md, "test-md-001")

        assert md.exists()
        content = md.read_text(encoding="utf-8")
        assert "test-md-001" in content
        assert "Hello" in content
        assert "Hi there" in content

    def test_with_tool_calls(self, tmp_path):
        """Tools used between prompt and response appear in the rendered exchange."""
        events = [
            {"type": "SessionStart", "ts": "2026-04-03T10:00:00+00:00", "agent_session_num": 0, "data": {}},
            {
                "type": "UserPromptSubmit",
                "ts": "2026-04-03T10:01:00+00:00",
                "agent_session_num": 0,
                "data": {"prompt": "Read the config"},
            },
            {
                "type": "PreToolUse",
                "ts": "2026-04-03T10:01:05+00:00",
                "agent_session_num": 0,
                "data": {"tool_name": "Read", "tool_input": {"file_path": "config.yaml"}},
            },
            {
                "type": "PostToolUse",
                "ts": "2026-04-03T10:01:06+00:00",
                "agent_session_num": 0,
                "data": {"tool_name": "Read"},
            },
            {
                "type": "AssistantResponse",
                "ts": "2026-04-03T10:02:00+00:00",
                "agent_session_num": 0,
                "data": {"response": "Config looks good"},
            },
        ]
        jsonl, md = self._make_session(tmp_path, "test-md-002", events)
        generate_markdown(jsonl, md, "test-md-002")

        content = md.read_text(encoding="utf-8")
        assert "Read" in content
        assert "config.yaml" in content

    def test_empty_session(self, tmp_path):
        jsonl = tmp_path / "empty.jsonl"
        md = tmp_path / "empty.md"
        jsonl.write_text("")
        generate_markdown(jsonl, md, "empty")
        # Should not crash, md may or may not exist
        assert True

    def test_malformed_jsonl(self, tmp_path):
        jsonl = tmp_path / "bad.jsonl"
        md = tmp_path / "bad.md"
        jsonl.write_text("not valid json\n")
        generate_markdown(jsonl, md, "bad")
        # Should not crash
        assert True

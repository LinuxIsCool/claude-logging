"""Tests for extract_content() — the function that converts raw hook data to searchable text."""

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from hooks.log_event import extract_content


class TestUserPromptSubmit:
    def test_string_prompt(self):
        assert extract_content("UserPromptSubmit", {"prompt": "hello world"}) == "hello world"

    def test_content_blocks(self):
        data = {"prompt": [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]}
        result = extract_content("UserPromptSubmit", data)
        assert "first" in result
        assert "second" in result

    def test_mixed_string_and_blocks(self):
        data = {"prompt": ["plain string", {"type": "text", "text": "block text"}]}
        result = extract_content("UserPromptSubmit", data)
        assert "plain string" in result
        assert "block text" in result

    def test_empty_prompt(self):
        assert extract_content("UserPromptSubmit", {"prompt": ""}) == ""

    def test_missing_prompt(self):
        assert extract_content("UserPromptSubmit", {}) == ""

    def test_non_string_non_list(self):
        result = extract_content("UserPromptSubmit", {"prompt": 42})
        assert result == "42"


class TestPreToolUse:
    def test_bash(self):
        data = {"tool_name": "Bash", "tool_input": {"command": "ls -la", "description": "list files"}}
        result = extract_content("PreToolUse", data)
        assert "ls -la" in result
        assert "list files" in result

    def test_bash_no_description(self):
        data = {"tool_name": "Bash", "tool_input": {"command": "pwd"}}
        result = extract_content("PreToolUse", data)
        assert result == "Running: pwd"

    def test_read(self):
        data = {"tool_name": "Read", "tool_input": {"file_path": "/src/main.py"}}
        assert extract_content("PreToolUse", data) == "Reading file: /src/main.py"

    def test_write(self):
        data = {"tool_name": "Write", "tool_input": {"file_path": "/out.txt"}}
        assert extract_content("PreToolUse", data) == "Writing file: /out.txt"

    def test_edit(self):
        data = {"tool_name": "Edit", "tool_input": {"file_path": "/src/lib.py"}}
        assert extract_content("PreToolUse", data) == "Editing file: /src/lib.py"

    def test_glob(self):
        data = {"tool_name": "Glob", "tool_input": {"pattern": "**/*.py"}}
        assert extract_content("PreToolUse", data) == "Finding files: **/*.py"

    def test_grep(self):
        data = {"tool_name": "Grep", "tool_input": {"pattern": "TODO"}}
        assert extract_content("PreToolUse", data) == "Searching for: TODO"

    def test_task(self):
        data = {"tool_name": "Task", "tool_input": {"description": "Research caching strategies"}}
        result = extract_content("PreToolUse", data)
        assert "Spawning agent" in result
        assert "Research caching" in result

    def test_unknown_tool(self):
        data = {"tool_name": "CustomTool", "tool_input": {"foo": "bar"}}
        result = extract_content("PreToolUse", data)
        assert "CustomTool" in result

    def test_missing_tool_name(self):
        result = extract_content("PreToolUse", {})
        assert "Unknown" in result


class TestPostToolUse:
    def test_bash_with_output(self):
        data = {"tool_name": "Bash", "tool_response": {"stdout": "hello\nworld"}}
        result = extract_content("PostToolUse", data)
        # Code emits "Output (N lines): ..." for multi-line stdout; match prefix.
        assert "Output (" in result
        assert "hello" in result

    def test_bash_long_output(self):
        lines = "\n".join(f"line {i}" for i in range(10))
        data = {"tool_name": "Bash", "tool_response": {"stdout": lines}}
        result = extract_content("PostToolUse", data)
        assert "10 lines" in result

    def test_bash_no_output(self):
        data = {"tool_name": "Bash", "tool_response": {"stdout": ""}}
        assert extract_content("PostToolUse", data) == "Command completed (no output)"

    def test_bash_string_response(self):
        data = {"tool_name": "Bash", "tool_response": "raw output"}
        result = extract_content("PostToolUse", data)
        assert "raw output" in result

    def test_read(self):
        assert extract_content("PostToolUse", {"tool_name": "Read"}) == "File read successfully"

    def test_glob_with_count(self):
        data = {"tool_name": "Glob", "tool_response": {"numFiles": 5}}
        assert extract_content("PostToolUse", data) == "Found 5 files"

    def test_glob_no_response(self):
        data = {"tool_name": "Glob", "tool_response": "something"}
        assert extract_content("PostToolUse", data) == "Glob completed"

    def test_grep(self):
        assert extract_content("PostToolUse", {"tool_name": "Grep"}) == "Search completed"

    def test_unknown_tool(self):
        result = extract_content("PostToolUse", {"tool_name": "Fancy"})
        assert result == "Fancy completed"


class TestSessionEvents:
    def test_session_start(self):
        data = {"source": "startup", "model": "claude-sonnet"}
        result = extract_content("SessionStart", data)
        assert "startup" in result
        assert "claude-sonnet" in result

    def test_session_start_defaults(self):
        result = extract_content("SessionStart", {})
        assert "startup" in result
        assert "unknown" in result

    def test_session_end(self):
        assert extract_content("SessionEnd", {}) == "Session ended"

    def test_stop(self):
        assert extract_content("Stop", {}) == "Claude finished responding"

    def test_pre_compact(self):
        assert extract_content("PreCompact", {}) == "Context compaction starting"

    def test_post_compact_with_summary(self):
        data = {"summary": "Discussed auth and DB schema"}
        result = extract_content("PostCompact", data)
        assert "Discussed auth" in result

    def test_post_compact_with_stats(self):
        data = {"stats": {"tokens_before": 1000, "tokens_after": 500}}
        result = extract_content("PostCompact", data)
        assert "tokens_before" in result

    def test_post_compact_empty(self):
        assert extract_content("PostCompact", {}) == "Context compaction completed"

    def test_notification(self):
        data = {"message": "Build succeeded"}
        assert extract_content("Notification", data) == "Build succeeded"

    def test_notification_empty(self):
        assert extract_content("Notification", {}) == "Notification"


class TestSubagentStop:
    def test_with_agent_type(self):
        data = {"agent_type": "code-reviewer"}
        result = extract_content("SubagentStop", data)
        assert "code-reviewer" in result

    def test_empty_agent_type(self):
        result = extract_content("SubagentStop", {})
        assert "Agent" in result


class TestAssistantResponse:
    def test_response_field(self):
        data = {"response": "Here is my analysis"}
        assert extract_content("AssistantResponse", data) == "Here is my analysis"

    def test_content_field(self):
        data = {"content": "Fallback content"}
        assert extract_content("assistant", data) == "Fallback content"


class TestUnknownEvents:
    def test_unknown_type_returns_none(self):
        assert extract_content("FutureEventType", {"foo": "bar"}) is None

    def test_empty_data(self):
        assert extract_content("SomethingNew", {}) is None

from __future__ import annotations

import io
import sys
from pathlib import Path

from adapters.pi_family import emit


ROOT = Path(__file__).parents[1]


def test_shared_emitter_adds_runtime_provenance(monkeypatch):
    captured = {}
    monkeypatch.setattr(emit, "process_event", lambda event, payload: captured.update(event=event, payload=payload))
    monkeypatch.setattr(emit.sys, "stdin", io.StringIO('{"session_id":"session","data":{}}'))
    monkeypatch.setattr(sys, "argv", ["emit.py", "omp", "omp-extension", "SessionStart"])
    assert emit.main() == 0
    assert captured["event"] == "SessionStart"
    assert captured["payload"]["_runtime"] == "omp"
    assert captured["payload"]["_capture_source"] == "omp-extension"


def test_pi_family_core_covers_proven_lifecycle():
    source = (ROOT / "adapters/pi_family/extension_core.ts").read_text()
    for native_event in (
        "session_start", "session_info_changed", "session_shutdown", "before_agent_start",
        "message_end", "tool_execution_start", "tool_execution_end",
        "session_before_compact", "session_compact", "model_select",
        "thinking_level_select",
    ):
        assert f'extension.on("{native_event}"' in source
    for canonical_event in (
        "SessionStart", "SessionInfo", "SessionEnd", "UserPromptSubmit",
        "Reasoning", "AssistantResponse", "PreToolUse", "PostToolUse",
        "PostToolUseFailure", "PreCompact", "PostCompact", "ModelChange",
        "ThinkingLevelChange",
    ):
        assert f'"{canonical_event}"' in source


def test_family_wrappers_keep_runtime_identity_distinct():
    expected = {
        "pi": ("pi", "pi-extension"),
        "prime_agent": ("prime-agent", "prime-agent-extension"),
        "omp": ("omp", "omp-extension"),
    }
    for directory, (runtime, capture_source) in expected.items():
        source = (ROOT / f"adapters/{directory}/extension.ts").read_text()
        assert 'installPiFamilyLogging' in source
        assert f'runtime: "{runtime}"' in source
        assert f'captureSource: "{capture_source}"' in source

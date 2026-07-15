"""Asserts claude-logging's contract with Claude Code itself.

claude-logging captured nothing from 2026-06-30 to 2026-07-15 because its
manifest sat at plugin.json instead of .claude-plugin/plugin.json. Claude Code
reads the manifest ONLY from .claude-plugin/plugin.json. Skills, commands and
agents load by directory convention, so they kept working and masked the
failure completely.

The structural tests run anywhere, including CI. The live test needs the
`claude` binary and is skipped without it.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"

# Every hook event the plugin intends to capture.
EXPECTED_HOOK_EVENTS = {
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "Notification",
    "Elicitation",
    "ElicitationResult",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PostToolUseFailure",
    "SubagentStart",
    "SubagentStop",
    "TeammateIdle",
    "TaskCreated",
    "TaskCompleted",
    "Stop",
    "StopFailure",
    "InstructionsLoaded",
    "ConfigChange",
    "CwdChanged",
    "FileChanged",
    "WorktreeCreate",
    "WorktreeRemove",
    "PreCompact",
    "PostCompact",
}


def test_manifest_exists_at_claude_plugin_path():
    assert MANIFEST.exists(), (
        "Claude Code reads the plugin manifest ONLY from "
        ".claude-plugin/plugin.json. A manifest at the repo root is silently "
        "ignored and hooks never register. This is exactly what caused the "
        "2026-06-30 outage."
    )


def test_manifest_is_valid_json_with_required_fields():
    m = json.loads(MANIFEST.read_text())
    for field in ("name", "version", "description"):
        assert field in m, f"manifest missing required field: {field}"


def test_manifest_declares_every_expected_hook_event():
    m = json.loads(MANIFEST.read_text())
    hooks = m.get("hooks")
    assert isinstance(hooks, dict), (
        "manifest.hooks must be an inline object of hook events. If it is a "
        "string pointing at ./hooks/hooks.json, that file already auto-loads "
        "and pointing at it triggers 'Duplicate hooks file detected' / "
        "hook-load-failed."
    )
    assert set(hooks) == EXPECTED_HOOK_EVENTS, (
        f"hook events drifted. missing={EXPECTED_HOOK_EVENTS - set(hooks)} "
        f"unexpected={set(hooks) - EXPECTED_HOOK_EVENTS}"
    )


def test_no_vestigial_root_manifest():
    assert not (PLUGIN_ROOT / "plugin.json").exists(), (
        "A root plugin.json is ignored by Claude Code. Keeping one alongside "
        ".claude-plugin/plugin.json invites edits to the dead file."
    )


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude binary not available")
def test_claude_plugin_validate_passes():
    r = subprocess.run(
        ["claude", "plugin", "validate", str(PLUGIN_ROOT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "✘" not in r.stdout, f"claude plugin validate failed:\n{r.stdout}"

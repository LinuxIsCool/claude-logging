#!/usr/bin/env python3
"""Hermes shell-hook bridge into Legion's runtime-neutral capture core."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from hooks.log_event import log_error, process_event  # noqa: E402


def normalize(payload: dict) -> tuple[str, dict] | None:
    native = payload.get("hook_event_name") or "unknown"
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    data: dict = {"hermes_event": native, **extra}
    canonical = {
        "on_session_start": "SessionStart",
        "on_session_finalize": "SessionEnd",
        "pre_llm_call": "UserPromptSubmit",
        "post_llm_call": "AssistantResponse",
        "pre_tool_call": "PreToolUse",
        "post_tool_call": "PostToolUseFailure" if extra.get("status") == "error" else "PostToolUse",
        "subagent_start": "SubagentStart",
        "subagent_stop": "SubagentStop",
        "post_api_request": "Usage",
    }.get(native)
    if canonical is None:
        return None
    if canonical == "UserPromptSubmit":
        data["prompt"] = extra.get("user_message") or ""
    elif canonical == "AssistantResponse":
        data["response"] = extra.get("assistant_response") or extra.get("response_text") or ""
    elif canonical == "PreToolUse":
        data.update(tool_name=payload.get("tool_name"), tool_input=payload.get("tool_input") or {}, tool_use_id=extra.get("tool_call_id"))
    elif canonical in ("PostToolUse", "PostToolUseFailure"):
        data.update(tool_name=payload.get("tool_name"), tool_input=payload.get("tool_input") or {}, tool_response=extra.get("result"), tool_use_id=extra.get("tool_call_id"), status=extra.get("status"), error_message=extra.get("error_message"))
    session_id = payload.get("session_id") or "unknown"
    return canonical, {
        "session_id": session_id,
        "cwd": payload.get("cwd"),
        "hook_event_name": native,
        "data": data,
        "turn_id": extra.get("turn_id"),
        "model": extra.get("model") or extra.get("response_model"),
        "duration_ms": extra.get("duration_ms") or (round(float(extra["api_duration"]) * 1000) if extra.get("api_duration") is not None else None),
        "tokens_in": (extra.get("usage") or {}).get("input_tokens") if isinstance(extra.get("usage"), dict) else None,
        "tokens_out": (extra.get("usage") or {}).get("output_tokens") if isinstance(extra.get("usage"), dict) else None,
        "_runtime": "hermes",
        "_capture_source": "hermes-shell-hook",
        "_source_kind": "synthetic" if session_id == "test-session" else "live",
    }


def main() -> int:
    try:
        normalized = normalize(json.load(sys.stdin))
        if normalized:
            process_event(*normalized)
    except Exception as exc:
        log_error(exc, "HermesAdapter")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

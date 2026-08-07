#!/usr/bin/env python3
"""Token accounting hook for claude-logging.

Reads a hook payload on STDIN and lands token/prompt metadata in logging.db.

    UserPromptSubmit  -> record the prompt and its context
    Stop / SubagentStop -> scan the transcript tail for assistant-turn usage

This hook is observation only. It writes nothing to STDOUT and always exits 0,
so a failure here can never block a turn or inject text into the context.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from lib import encode_project_path  # noqa: E402
from lib.prompt_feed import render_feed  # noqa: E402
from lib.token_meter import (  # noqa: E402
    classify_prompts,
    open_db,
    record_prompt,
    scan_transcript,
)


def storage_path(cwd: str | None) -> Path:
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or cwd or os.getcwd()
    return Path.home() / ".claude" / "local" / "logging" / encode_project_path(project_dir)


def git_branch(cwd: str | None) -> str | None:
    if not cwd or not Path(cwd).is_dir():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-e", "--event", required=True)
    args = ap.parse_args()

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    conn = None
    try:
        conn = open_db(storage_path(payload.get("cwd")))
        if args.event == "UserPromptSubmit":
            record_prompt(conn, payload, git_branch=git_branch(payload.get("cwd")))
        else:
            sid = payload.get("session_id") or ""
            tp = payload.get("transcript_path") or ""
            scan_transcript(conn, sid, tp)
            # Subagent spend lands in sibling files the hook payload never
            # names. Without this the fan-out, which is most of the cost, is
            # invisible.
            main = Path(os.path.expanduser(tp))
            if main.name.endswith(".jsonl"):
                for sub in sorted(main.parent.glob(f"{main.stem}/subagents/*.jsonl")):
                    scan_transcript(conn, sid, str(sub), sidechain=True)
            classify_prompts(conn)
            conn.close()
            conn = None
            # Refresh the feed, but only where one already exists. A machine
            # that never set one up stays untouched.
            slug = encode_project_path(
                os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
            )
            render_feed(slug, only_if_exists=True)
    except Exception as exc:  # never break the session over accounting
        if os.environ.get("CLAUDE_LOGGING_DEBUG"):
            print(f"token_meter: {exc}", file=sys.stderr)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

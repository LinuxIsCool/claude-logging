#!/usr/bin/env python3
"""Thin Codex hook adapter for Legion's runtime-neutral logging core."""

import argparse
import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from hooks.log_event import log_error, process_event  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture a Codex lifecycle event")
    parser.add_argument("event", help="Normalized lifecycle event name")
    args = parser.parse_args()

    try:
        payload = json.load(sys.stdin)
        payload["_runtime"] = "codex"
        payload["_capture_source"] = "codex-hook"
        process_event(args.event, payload)
    except Exception as exc:
        # Observability must never block the host runtime.
        log_error(exc, f"CodexAdapter:{args.event}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Thin Pi extension adapter for the runtime-neutral logging core."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from hooks.log_event import log_error, process_event  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("event")
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        payload["_runtime"] = "pi"
        payload["_capture_source"] = "pi-extension"
        process_event(args.event, payload)
    except Exception as exc:
        log_error(exc, f"PiAdapter:{args.event}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

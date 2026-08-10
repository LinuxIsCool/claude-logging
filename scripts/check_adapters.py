#!/usr/bin/env python3
"""Run adapter registry conformance checks."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib.adapter_conformance import load_registry, validate_registry  # noqa: E402


def main() -> int:
    registry = load_registry(ROOT / "adapters" / "registry.json")
    errors = validate_registry(registry, ROOT)
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    for adapter in registry["adapters"]:
        capabilities = ", ".join(adapter["capabilities"])
        print(f"PASS {adapter['runtime']}: {capabilities}")
        for gap in adapter["known_gaps"]:
            print(f"  GAP: {gap}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

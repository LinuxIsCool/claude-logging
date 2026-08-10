"""Validation for the machine-readable runtime adapter registry."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CONTRACT = "legion.logging.adapter.v1"
RUNTIME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
KNOWN_CAPABILITIES = {
    "messages", "reasoning", "tools", "tool_correlation", "tokens", "costs",
    "models", "compaction", "session_tree", "subagents", "session_lineage",
    "permissions", "artifacts",
}


def load_registry(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def validate_registry(registry: dict[str, Any], root: Path | None = None) -> list[str]:
    errors: list[str] = []
    if registry.get("contract") != CONTRACT:
        errors.append(f"contract must be {CONTRACT}")
    adapters = registry.get("adapters")
    if not isinstance(adapters, list):
        return errors + ["adapters must be a list"]
    runtimes: set[str] = set()
    for index, adapter in enumerate(adapters):
        label = f"adapters[{index}]"
        runtime = adapter.get("runtime") if isinstance(adapter, dict) else None
        if not isinstance(runtime, str) or not RUNTIME_PATTERN.fullmatch(runtime):
            errors.append(f"{label}.runtime is invalid")
            continue
        if runtime in runtimes:
            errors.append(f"duplicate runtime: {runtime}")
        runtimes.add(runtime)
        for flag in ("live", "archive"):
            if not isinstance(adapter.get(flag), bool):
                errors.append(f"{runtime}.{flag} must be boolean")
        capabilities = adapter.get("capabilities")
        if not isinstance(capabilities, list):
            errors.append(f"{runtime}.capabilities must be a list")
        else:
            unknown = sorted(set(capabilities) - KNOWN_CAPABILITIES)
            if unknown:
                errors.append(f"{runtime} has unknown capabilities: {', '.join(unknown)}")
            if "tool_correlation" in capabilities and "tools" not in capabilities:
                errors.append(f"{runtime} declares tool_correlation without tools")
        extension = adapter.get("extension")
        if adapter.get("live") and not extension:
            errors.append(f"{runtime} is live but has no extension")
        if extension and root is not None and not (root / extension).is_file():
            errors.append(f"{runtime} extension does not exist: {extension}")
        if not isinstance(adapter.get("known_gaps"), list):
            errors.append(f"{runtime}.known_gaps must be a list")
    return errors

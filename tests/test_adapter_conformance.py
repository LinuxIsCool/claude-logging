from pathlib import Path

from lib.adapter_conformance import load_registry, validate_registry


ROOT = Path(__file__).parents[1]


def test_checked_in_adapter_registry_conforms():
    registry = load_registry(ROOT / "adapters/registry.json")
    assert validate_registry(registry, ROOT) == []


def test_registry_rejects_tool_correlation_without_tools():
    registry = {
        "contract": "legion.logging.adapter.v1",
        "adapters": [{
            "runtime": "bad", "family": "test", "live": False, "archive": True,
            "extension": None, "session_glob": "x", "capabilities": ["tool_correlation"],
            "known_gaps": [],
        }],
    }
    assert "bad declares tool_correlation without tools" in validate_registry(registry)

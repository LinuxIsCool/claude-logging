"""Contract fixtures proving Claude and Codex share one canonical envelope."""
from __future__ import annotations

import pytest

from lib.event_contract import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    CanonicalEventV1,
    ContractError,
    EventCategory,
    validate_event_v1,
)


@pytest.fixture(params=["claude", "codex"])
def runtime_event(request: pytest.FixtureRequest) -> dict:
    runtime = str(request.param)
    return {
        "id": f"evt-{runtime}",
        "session_id": f"session-{runtime}",
        "type": "UserPromptSubmit",
        "ts": "2026-08-09T19:00:00+00:00",
        "runtime": runtime,
        "runtime_event": "UserPromptSubmit" if runtime == "claude" else "turn.started",
        "capture_source": f"{runtime}-hook",
        "source_kind": "live",
        "turn_id": "turn-1" if runtime == "codex" else None,
        "data": {"prompt": "Inspect the logging contract"},
    }


def test_runtime_fixtures_satisfy_same_contract(runtime_event: dict) -> None:
    validate_event_v1(runtime_event)
    normalized = CanonicalEventV1.from_mapping(runtime_event)
    assert normalized.contract == f"{CONTRACT_NAME}.v{CONTRACT_VERSION}"
    assert normalized.category is EventCategory.MESSAGE
    assert normalized.runtime == runtime_event["runtime"]
    assert normalized.runtime_event == runtime_event["runtime_event"]
    assert normalized.source_kind.value == "live"


def test_native_event_name_does_not_replace_canonical_type() -> None:
    event = {
        "id": "evt-codex",
        "session_id": "session-codex",
        "type": "PreToolUse",
        "ts": "2026-08-09T19:00:00+00:00",
        "runtime": "codex",
        "runtime_event": "item.started",
        "capture_source": "codex-hook",
        "source_kind": "live",
        "data": {},
    }
    normalized = CanonicalEventV1.from_mapping(event)
    assert normalized.event_type == "PreToolUse"
    assert normalized.runtime_event == "item.started"
    assert normalized.category is EventCategory.ACTION


@pytest.mark.parametrize("field", ["id", "session_id", "type", "ts", "runtime", "runtime_event", "capture_source", "source_kind"])
def test_required_fields_fail_closed(field: str, runtime_event: dict) -> None:
    runtime_event[field] = ""
    with pytest.raises(ContractError, match=field):
        validate_event_v1(runtime_event)


def test_runtime_identifier_is_stable_and_machine_readable(runtime_event: dict) -> None:
    runtime_event["runtime"] = "Codex Local"
    with pytest.raises(ContractError, match="lowercase"):
        validate_event_v1(runtime_event)


def test_source_kind_is_closed_vocabulary(runtime_event: dict) -> None:
    runtime_event["source_kind"] = "probably-live"
    with pytest.raises(ContractError, match="source_kind"):
        validate_event_v1(runtime_event)

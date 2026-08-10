"""Versioned runtime-neutral event contract for Legion logging.

The persisted v1 envelope predates this module, so the version is defined by
its required fields rather than by adding another database column.  Adapters
may add fields, but every event crossing the shared capture boundary must pass
``validate_event_v1`` before it is written.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


CONTRACT_NAME = "legion.logging.event"
CONTRACT_VERSION = 1


class EventCategory(StrEnum):
    SESSION = "session"
    MESSAGE = "message"
    ACTION = "action"
    PERMISSION = "permission"
    SUBAGENT = "subagent"
    CONTEXT = "context"
    NOTIFICATION = "notification"
    FAILURE = "failure"
    OTHER = "other"


class SourceKind(StrEnum):
    LIVE = "live"
    ARCHIVE = "archive"
    BACKFILL = "backfill"
    SYNTHETIC = "synthetic"


_CATEGORIES = {
    "SessionStart": EventCategory.SESSION,
    "SessionEnd": EventCategory.SESSION,
    "UserPromptSubmit": EventCategory.MESSAGE,
    "AssistantResponse": EventCategory.MESSAGE,
    "PreToolUse": EventCategory.ACTION,
    "PostToolUse": EventCategory.ACTION,
    "PostToolUseFailure": EventCategory.FAILURE,
    "PermissionRequest": EventCategory.PERMISSION,
    "SubagentStart": EventCategory.SUBAGENT,
    "SubagentStop": EventCategory.SUBAGENT,
    "PreCompact": EventCategory.CONTEXT,
    "PostCompact": EventCategory.CONTEXT,
    "Notification": EventCategory.NOTIFICATION,
}


class ContractError(ValueError):
    """Raised when an adapter produces an invalid canonical envelope."""


@dataclass(frozen=True)
class CanonicalEventV1:
    event_id: str
    session_id: str
    event_type: str
    timestamp: str
    runtime: str
    runtime_event: str
    capture_source: str
    source_kind: SourceKind
    category: EventCategory
    turn_id: str | None = None
    model: str | None = None
    permission_mode: str | None = None

    @property
    def contract(self) -> str:
        return f"{CONTRACT_NAME}.v{CONTRACT_VERSION}"

    @classmethod
    def from_mapping(cls, event: Mapping[str, Any]) -> "CanonicalEventV1":
        validate_event_v1(event)
        event_type = str(event["type"])
        return cls(
            event_id=str(event["id"]),
            session_id=str(event["session_id"]),
            event_type=event_type,
            timestamp=str(event["ts"]),
            runtime=str(event["runtime"]),
            runtime_event=str(event["runtime_event"]),
            capture_source=str(event["capture_source"]),
            source_kind=SourceKind(str(event["source_kind"])),
            category=_CATEGORIES.get(event_type, EventCategory.OTHER),
            turn_id=_optional_string(event.get("turn_id")),
            model=_optional_string(event.get("model")),
            permission_mode=_optional_string(event.get("permission_mode")),
        )


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def validate_event_v1(event: Mapping[str, Any]) -> None:
    """Validate the stable minimum shared by Claude, Codex, and future adapters."""
    required = ("id", "session_id", "type", "ts", "runtime", "runtime_event", "capture_source", "source_kind")
    missing = [name for name in required if not isinstance(event.get(name), str) or not event[name].strip()]
    if missing:
        raise ContractError(f"invalid {CONTRACT_NAME}.v{CONTRACT_VERSION}: missing {', '.join(missing)}")
    runtime = str(event["runtime"])
    if runtime != runtime.lower() or any(char.isspace() for char in runtime):
        raise ContractError("runtime must be a lowercase, whitespace-free identifier")
    try:
        SourceKind(str(event["source_kind"]))
    except ValueError as exc:
        allowed = ", ".join(kind.value for kind in SourceKind)
        raise ContractError(f"source_kind must be one of: {allowed}") from exc
    data = event.get("data")
    if data is not None and not isinstance(data, Mapping):
        raise ContractError("data must be an object when present")

"""
Claude Code Native Data Types

Dataclass definitions for Claude Code's internal data structures.
Used for parsing ~/.claude/ directory contents and translating to logging events.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime


# Type aliases for readability
MessageType = Literal[
    "user", "assistant", "progress", "system",
    "summary", "file-history-snapshot", "queue-operation"
]

UserType = Literal["external", "internal"]


@dataclass
class ContentBlock:
    """A content block within a message."""
    type: str  # "text", "image", "tool_use", "tool_result", "thinking"
    text: Optional[str] = None
    thinking: Optional[str] = None
    # Tool use fields
    id: Optional[str] = None
    name: Optional[str] = None
    input: Optional[Dict[str, Any]] = None
    # Tool result fields
    tool_use_id: Optional[str] = None
    content: Optional[Any] = None
    # Image fields
    source: Optional[Dict[str, Any]] = None


@dataclass
class Message:
    """The message payload within a transcript entry."""
    role: str  # "user" or "assistant"
    content: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class TranscriptEntry:
    """
    A single entry from a Claude Code JSONL transcript.

    Location: ~/.claude/projects/{encoded-path}/{session-id}.jsonl
    """
    type: MessageType
    uuid: str
    timestamp: str  # ISO-8601
    sessionId: str
    parentUuid: Optional[str] = None
    isSidechain: bool = False
    userType: Optional[UserType] = None
    cwd: Optional[str] = None
    version: Optional[str] = None  # Claude Code version e.g., "2.1.21"
    gitBranch: Optional[str] = None
    model: Optional[str] = None  # e.g., "claude-opus-4-5-20251101"
    message: Optional[Dict[str, Any]] = None
    thinkingMetadata: Optional[Dict[str, Any]] = None
    permissionMode: Optional[str] = None
    todos: List[Dict] = field(default_factory=list)
    # Additional fields that may appear
    leafUuid: Optional[str] = None  # For summary messages
    snapshot: Optional[Dict] = None  # For file-history-snapshot

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TranscriptEntry":
        """Create from parsed JSON dict."""
        return cls(
            type=data.get("type", "user"),
            uuid=data.get("uuid", ""),
            timestamp=data.get("timestamp", ""),
            sessionId=data.get("sessionId", ""),
            parentUuid=data.get("parentUuid"),
            isSidechain=data.get("isSidechain", False),
            userType=data.get("userType"),
            cwd=data.get("cwd"),
            version=data.get("version"),
            gitBranch=data.get("gitBranch"),
            model=data.get("model"),
            message=data.get("message"),
            thinkingMetadata=data.get("thinkingMetadata"),
            permissionMode=data.get("permissionMode"),
            todos=data.get("todos", []),
            leafUuid=data.get("leafUuid"),
            snapshot=data.get("snapshot"),
        )

    def get_text_content(self) -> str:
        """Extract text content from message blocks."""
        if not self.message:
            return ""

        content = self.message.get("content", [])
        if isinstance(content, str):
            return content

        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
        return "\n".join(texts)

    def get_tool_uses(self) -> List[Dict[str, Any]]:
        """Extract tool_use blocks from assistant message."""
        if not self.message or self.type != "assistant":
            return []

        content = self.message.get("content", [])
        if isinstance(content, str):
            return []

        return [
            block for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]

    def is_external_user(self) -> bool:
        """Check if this is an external user message (actual user prompt)."""
        return self.type == "user" and self.userType == "external"


@dataclass
class SessionIndexEntry:
    """
    Entry in sessions-index.json.

    Location: ~/.claude/projects/{encoded-path}/sessions-index.json
    """
    sessionId: str
    fullPath: str
    fileMtime: int  # Unix timestamp
    messageCount: int
    created: str
    modified: str
    projectPath: str
    gitBranch: Optional[str] = None
    firstPrompt: Optional[str] = None
    summary: Optional[str] = None
    isSidechain: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionIndexEntry":
        return cls(
            sessionId=data.get("sessionId", ""),
            fullPath=data.get("fullPath", ""),
            fileMtime=data.get("fileMtime", 0),
            messageCount=data.get("messageCount", 0),
            created=data.get("created", ""),
            modified=data.get("modified", ""),
            projectPath=data.get("projectPath", ""),
            gitBranch=data.get("gitBranch"),
            firstPrompt=data.get("firstPrompt"),
            summary=data.get("summary"),
            isSidechain=data.get("isSidechain", False),
        )


@dataclass
class InstanceRecord:
    """
    Claude Code instance registration.

    Location: ~/.claude/instances/registry.json
    """
    name: str
    model: str
    task: str
    cwd: str
    created: str
    last_seen: str
    status: Literal["active", "inactive"] = "active"
    process_number: int = 0
    pane_id: Optional[str] = None
    pane_ref: Optional[str] = None
    auto_named: bool = False
    continued: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InstanceRecord":
        return cls(
            name=data.get("name", "Claude"),
            model=data.get("model", ""),
            task=data.get("task", ""),
            cwd=data.get("cwd", ""),
            created=data.get("created", ""),
            last_seen=data.get("last_seen", ""),
            status=data.get("status", "active"),
            process_number=data.get("process_number", 0),
            pane_id=data.get("pane_id"),
            pane_ref=data.get("pane_ref"),
            auto_named=data.get("auto_named", False),
            continued=data.get("continued", False),
        )


@dataclass
class StatuslineEvent:
    """
    Statusline event from statusline.jsonl.

    Location: ~/.claude/instances/statusline.jsonl
    """
    ts: str
    session: str  # 8-char abbreviated session UUID
    type: str  # claude_input, statusline_render, summary, etc.
    value: Any
    ok: bool = True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StatuslineEvent":
        return cls(
            ts=data.get("ts", ""),
            session=data.get("session", ""),
            type=data.get("type", ""),
            value=data.get("value"),
            ok=data.get("ok", True),
        )

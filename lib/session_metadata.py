"""
Session Metadata & Agent Card Models

Provides enriched session metadata that bridges:
- Claude Code native data (transcripts, tasks)
- Statusline plugin data (identity, cost, context)
- A2A Protocol concepts (Agent Cards, Tasks, Capabilities)
- ElizaOS concepts (World, Room, Memory)

This enables Claude instances to be treated as discoverable agents
with rich metadata for search, analytics, and future integrations.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime
from enum import Enum
import json


class SessionStatus(str, Enum):
    """Session lifecycle status."""
    ACTIVE = "active"
    IDLE = "idle"
    COMPLETED = "completed"
    COMPACTED = "compacted"


class RoomType(str, Enum):
    """ElizaOS-inspired room categorization."""
    MAIN = "main"           # Primary session
    SUBAGENT = "subagent"   # Spawned agent
    SIDECHAIN = "sidechain" # Branched conversation
    BACKGROUND = "background"  # Background task


@dataclass
class AgentCapabilities:
    """
    A2A-inspired capability declaration.

    Maps to A2A Agent Card 'skills' and 'capabilities' fields.
    """
    tools_used: List[str] = field(default_factory=list)
    tool_frequency: Dict[str, int] = field(default_factory=dict)
    languages: List[str] = field(default_factory=list)  # Programming languages
    domains: List[str] = field(default_factory=list)    # e.g., "web", "cli", "data"
    streaming: bool = True
    multi_turn: bool = True
    file_access: bool = True
    web_access: bool = False
    code_execution: bool = True


@dataclass
class CostMetrics:
    """Token and cost tracking for the session."""
    total_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    context_percentage: int = 0  # 0-100
    peak_context_percentage: int = 0


@dataclass
class GitContext:
    """Git repository context for the session."""
    branch: Optional[str] = None
    lines_added: int = 0
    lines_removed: int = 0
    is_dirty: bool = False
    last_commit: Optional[str] = None


@dataclass
class AgentCard:
    """
    A2A Protocol-compatible Agent Card.

    This is the external-facing identity that can be used for:
    - Agent discovery and registration
    - Capability advertisement
    - Cross-system integration

    See: https://a2a-protocol.org/latest/specification/
    """
    # Core identity (A2A required)
    name: str
    description: str = ""

    # A2A 'skills' - what the agent can do
    skills: List[str] = field(default_factory=list)

    # A2A 'capabilities' - technical abilities
    capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)

    # Endpoint (for A2A communication)
    endpoint: str = "local://claude-code"

    # Model info
    model_id: str = ""
    model_display_name: str = ""

    # Metadata
    created_at: str = ""
    last_active: str = ""
    session_count: int = 0
    total_interactions: int = 0

    def to_a2a_json(self) -> Dict[str, Any]:
        """Export as A2A-compatible JSON."""
        return {
            "name": self.name,
            "description": self.description,
            "skills": self.skills,
            "endpoint": self.endpoint,
            "capabilities": {
                "streaming": self.capabilities.streaming,
                "multiTurn": self.capabilities.multi_turn,
                "tools": self.capabilities.tools_used,
            },
            "metadata": {
                "model": self.model_id,
                "modelDisplayName": self.model_display_name,
                "createdAt": self.created_at,
                "lastActive": self.last_active,
                "sessionCount": self.session_count,
            }
        }


@dataclass
class SessionMetadata:
    """
    Enriched session metadata combining all data sources.

    This is the primary data model for session tracking, combining:
    - Claude Code native session data
    - Statusline plugin enrichments
    - A2A-compatible fields
    - ElizaOS-inspired organization
    """
    # === Core Identity ===
    session_id: str
    name: Optional[str] = None          # Auto-generated name (Archivist, Spark, etc.)
    description: Optional[str] = None   # 2-5 word role description
    summary: Optional[str] = None       # Current task summary

    # === A2A Task Fields ===
    task_status: SessionStatus = SessionStatus.ACTIVE
    context_id: Optional[str] = None    # Groups related sessions
    parent_session_id: Optional[str] = None  # For subagents

    # === ElizaOS-Inspired Fields ===
    world_id: str = ""                  # Project path (cwd)
    room_type: RoomType = RoomType.MAIN
    memory_refs: List[str] = field(default_factory=list)  # Links to memory systems

    # === Model & Environment ===
    model: str = ""
    model_display_name: str = ""
    claude_code_version: str = ""
    cwd: str = ""

    # === Timing ===
    started_at: str = ""
    ended_at: Optional[str] = None
    duration_seconds: int = 0
    last_activity: str = ""

    # === Metrics ===
    event_count: int = 0
    prompt_count: int = 0
    agent_session_num: int = 0          # Context reset counter
    cost: CostMetrics = field(default_factory=CostMetrics)
    git: GitContext = field(default_factory=GitContext)

    # === Capabilities (derived from usage) ===
    capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)

    # === Integration ===
    process_number: Optional[int] = None  # tmux process number
    pane_id: Optional[str] = None         # tmux pane ID
    pane_ref: Optional[str] = None        # tmux pane reference

    # === Tags & Classification ===
    tags: List[str] = field(default_factory=list)
    auto_named: bool = False

    def to_agent_card(self) -> AgentCard:
        """Generate an A2A Agent Card from session metadata."""
        # Derive skills from tool usage
        skills = []
        if "Read" in self.capabilities.tools_used or "Grep" in self.capabilities.tools_used:
            skills.append("code_analysis")
        if "Write" in self.capabilities.tools_used or "Edit" in self.capabilities.tools_used:
            skills.append("code_generation")
        if "Bash" in self.capabilities.tools_used:
            skills.append("system_operations")
        if "WebSearch" in self.capabilities.tools_used or "WebFetch" in self.capabilities.tools_used:
            skills.append("research")
        if "Task" in self.capabilities.tools_used:
            skills.append("delegation")

        return AgentCard(
            name=self.name or "Claude",
            description=self.description or self.summary or "",
            skills=skills,
            capabilities=self.capabilities,
            model_id=self.model,
            model_display_name=self.model_display_name,
            created_at=self.started_at,
            last_active=self.last_activity,
            session_count=1,
            total_interactions=self.prompt_count,
        )

    @classmethod
    def from_statusline_events(
        cls,
        session_id: str,
        events: List[Dict[str, Any]]
    ) -> "SessionMetadata":
        """
        Create SessionMetadata from statusline events.

        Aggregates statusline_render and claude_input events to build
        comprehensive session metadata.
        """
        metadata = cls(session_id=session_id)

        # Track latest values
        latest_render = None
        latest_input = None

        for event in events:
            event_type = event.get("type")
            value = event.get("value", {})
            ts = event.get("ts", "")

            if event_type == "statusline_render" and isinstance(value, dict):
                latest_render = value
                metadata.name = value.get("name")
                metadata.description = value.get("description")
                metadata.summary = value.get("summary")
                metadata.model_display_name = value.get("model", "")
                metadata.cwd = value.get("cwd", "")
                metadata.cost.context_percentage = value.get("context_pct", 0)
                metadata.cost.peak_context_percentage = max(
                    metadata.cost.peak_context_percentage,
                    value.get("context_pct", 0)
                )

                # Parse cost
                try:
                    metadata.cost.total_cost_usd = float(value.get("cost", "0"))
                except (ValueError, TypeError):
                    pass

                # Parse prompt count
                try:
                    metadata.prompt_count = int(value.get("prompt_count", "0"))
                except (ValueError, TypeError):
                    pass

                # Parse agent session
                try:
                    metadata.agent_session_num = int(value.get("agent_session", "0"))
                except (ValueError, TypeError):
                    pass

                # Parse process number
                try:
                    metadata.process_number = int(value.get("process_num", "0"))
                except (ValueError, TypeError):
                    pass

                # Git context
                if value.get("branch"):
                    metadata.git.branch = value.get("branch")
                    metadata.git.is_dirty = value.get("git_dirty") == "yes"

                    # Parse git stats like "+23/-0"
                    git_stats = value.get("git_stats", "")
                    if git_stats:
                        parts = git_stats.split("/")
                        if len(parts) == 2:
                            try:
                                metadata.git.lines_added = int(parts[0].replace("+", ""))
                                metadata.git.lines_removed = int(parts[1].replace("-", ""))
                            except ValueError:
                                pass

                metadata.last_activity = ts

            elif event_type == "claude_input" and isinstance(value, dict):
                latest_input = value
                metadata.model = value.get("model", {}).get("id", "")
                metadata.model_display_name = value.get("model", {}).get("display_name", "")
                metadata.claude_code_version = value.get("version", "")
                metadata.cwd = value.get("cwd", metadata.cwd)

                # Cost details
                cost_data = value.get("cost", {})
                if cost_data:
                    metadata.cost.total_cost_usd = cost_data.get("total_cost_usd", 0)
                    metadata.git.lines_added = cost_data.get("total_lines_added", 0)
                    metadata.git.lines_removed = cost_data.get("total_lines_removed", 0)

                # Context window
                ctx = value.get("context_window", {})
                if ctx:
                    metadata.cost.input_tokens = ctx.get("total_input_tokens", 0)
                    metadata.cost.output_tokens = ctx.get("total_output_tokens", 0)
                    usage = ctx.get("current_usage") or {}
                    metadata.cost.cache_write_tokens = usage.get("cache_creation_input_tokens", 0)
                    metadata.cost.cache_read_tokens = usage.get("cache_read_input_tokens", 0)

            elif event_type == "session_start":
                metadata.started_at = ts

            elif event_type == "name" and isinstance(value, str):
                metadata.name = value
                metadata.auto_named = True

            elif event_type == "summary" and isinstance(value, str):
                metadata.summary = value

            elif event_type == "description" and isinstance(value, str):
                metadata.description = value

        # Set world_id from cwd
        metadata.world_id = metadata.cwd

        return metadata


class SessionMetadataStore:
    """
    Storage and retrieval for session metadata.

    Can be backed by SQLite, in-memory, or external stores.
    """

    def __init__(self, db_connection=None):
        self.conn = db_connection
        self._cache: Dict[str, SessionMetadata] = {}

    def get(self, session_id: str) -> Optional[SessionMetadata]:
        """Get metadata for a session."""
        if session_id in self._cache:
            return self._cache[session_id]

        if self.conn:
            # TODO: Load from SQLite session_metadata table
            pass

        return None

    def save(self, metadata: SessionMetadata) -> None:
        """Save session metadata."""
        self._cache[metadata.session_id] = metadata

        if self.conn:
            # TODO: Persist to SQLite session_metadata table
            pass

    def search(
        self,
        name: Optional[str] = None,
        model: Optional[str] = None,
        status: Optional[SessionStatus] = None,
        tags: Optional[List[str]] = None,
        limit: int = 50
    ) -> List[SessionMetadata]:
        """Search sessions by criteria."""
        results = []

        for metadata in self._cache.values():
            if name and name.lower() not in (metadata.name or "").lower():
                continue
            if model and model not in metadata.model:
                continue
            if status and metadata.task_status != status:
                continue
            if tags and not any(t in metadata.tags for t in tags):
                continue

            results.append(metadata)

            if len(results) >= limit:
                break

        return results

    def get_agent_cards(self, limit: int = 50) -> List[AgentCard]:
        """Get Agent Cards for all sessions."""
        cards = []
        for metadata in list(self._cache.values())[:limit]:
            cards.append(metadata.to_agent_card())
        return cards

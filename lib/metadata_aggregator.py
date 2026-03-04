"""
Metadata Aggregator

Aggregates data from multiple sources into unified SessionMetadata:
- Statusline events (identity, cost, context)
- Logging events (tools, interactions)
- Instance registry (process info)

Provides real-time and batch aggregation capabilities.
"""

import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List, Iterator
from datetime import datetime, timezone
from collections import defaultdict

from .session_metadata import (
    SessionMetadata,
    SessionStatus,
    RoomType,
    AgentCapabilities,
    CostMetrics,
    GitContext,
    AgentCard,
    SessionMetadataStore,
)

logger = logging.getLogger(__name__)


class MetadataAggregator:
    """
    Aggregates session metadata from multiple sources.

    Sources:
    1. ~/.claude/instances/statusline.jsonl - Rich UI state snapshots
    2. ~/.claude/instances/registry.json - Instance registry
    3. Logging plugin events - Tool usage, interactions
    """

    def __init__(
        self,
        claude_dir: Optional[Path] = None,
        storage=None,
        store: Optional[SessionMetadataStore] = None
    ):
        self.claude_dir = claude_dir or Path.home() / ".claude"
        self.instances_dir = self.claude_dir / "instances"
        self.storage = storage  # StorageManager for logging events
        self.store = store or SessionMetadataStore()

        # Cache statusline events by session
        self._statusline_cache: Dict[str, List[Dict]] = defaultdict(list)
        self._last_statusline_position: int = 0

    def load_statusline_events(self, incremental: bool = True) -> int:
        """
        Load statusline events into cache.

        Args:
            incremental: If True, only load new events since last read

        Returns:
            Number of events loaded
        """
        statusline_path = self.instances_dir / "statusline.jsonl"
        if not statusline_path.exists():
            return 0

        loaded = 0
        start_pos = self._last_statusline_position if incremental else 0

        try:
            with open(statusline_path, "r") as f:
                if start_pos > 0:
                    f.seek(start_pos)

                for line in f:
                    if line.strip():
                        try:
                            event = json.loads(line)
                            session = event.get("session", "")
                            if session:
                                self._statusline_cache[session].append(event)
                                loaded += 1
                        except json.JSONDecodeError:
                            continue

                self._last_statusline_position = f.tell()

        except Exception as e:
            logger.error(f"Error loading statusline events: {e}")

        logger.debug(f"Loaded {loaded} statusline events")
        return loaded

    def load_instance_registry(self) -> Dict[str, Dict]:
        """Load the instance registry."""
        registry_path = self.instances_dir / "registry.json"
        if not registry_path.exists():
            return {}

        try:
            with open(registry_path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading registry: {e}")
            return {}

    def aggregate_session(self, session_id: str) -> Optional[SessionMetadata]:
        """
        Aggregate metadata for a specific session.

        Combines:
        - Statusline events (by short session ID prefix)
        - Instance registry entry
        - Logging events (tool usage)
        """
        # Get short session ID (8 chars) used by statusline
        short_id = session_id[:8] if len(session_id) > 8 else session_id

        # Get statusline events
        events = self._statusline_cache.get(short_id, [])

        if not events:
            # Try to find in registry
            registry = self.load_instance_registry()
            if session_id not in registry:
                return None

            # Create basic metadata from registry
            entry = registry[session_id]
            metadata = SessionMetadata(
                session_id=session_id,
                name=entry.get("name"),
                description=entry.get("task"),
                model_display_name=entry.get("model"),
                cwd=entry.get("cwd", ""),
                started_at=entry.get("created", ""),
                last_activity=entry.get("last_seen", ""),
                process_number=entry.get("process_number"),
                pane_id=entry.get("pane_id"),
                pane_ref=entry.get("pane_ref"),
                auto_named=entry.get("auto_named", False),
                task_status=SessionStatus.ACTIVE if entry.get("status") == "active" else SessionStatus.COMPLETED,
            )
        else:
            # Create from statusline events
            metadata = SessionMetadata.from_statusline_events(session_id, events)

        # Enrich with tool usage from logging events
        if self.storage:
            metadata.capabilities = self._extract_capabilities(session_id)
            metadata.event_count = self._count_events(session_id)

        # Determine room type
        if metadata.parent_session_id:
            metadata.room_type = RoomType.SUBAGENT

        # Save to store
        self.store.save(metadata)

        return metadata

    def aggregate_all(self) -> List[SessionMetadata]:
        """
        Aggregate metadata for all known sessions.

        Combines:
        - All sessions from statusline
        - All sessions from registry
        - All sessions from logging database
        """
        # Ensure statusline is loaded
        self.load_statusline_events()

        # Collect all session IDs
        session_ids = set()

        # From statusline (short IDs)
        for short_id in self._statusline_cache.keys():
            session_ids.add(short_id)

        # From registry (full IDs)
        registry = self.load_instance_registry()
        for full_id in registry.keys():
            session_ids.add(full_id)
            session_ids.add(full_id[:8])  # Also add short version

        # From logging database
        if self.storage:
            try:
                cursor = self.storage.sqlite.conn.execute(
                    "SELECT DISTINCT session_id FROM sessions"
                )
                for row in cursor:
                    session_ids.add(row[0])
                    session_ids.add(row[0][:8])
            except Exception:
                pass

        # Aggregate each session
        results = []
        for sid in session_ids:
            try:
                metadata = self.aggregate_session(sid)
                if metadata:
                    results.append(metadata)
            except Exception as e:
                logger.error(f"Error aggregating session {sid}: {e}")

        logger.info(f"Aggregated {len(results)} sessions")
        return results

    def _extract_capabilities(self, session_id: str) -> AgentCapabilities:
        """Extract capabilities from logging events."""
        caps = AgentCapabilities()

        if not self.storage:
            return caps

        try:
            # Get tool usage from events
            cursor = self.storage.sqlite.conn.execute("""
                SELECT type, data FROM events
                WHERE session_id = ? AND type = 'PreToolUse'
            """, (session_id,))

            tool_counts = defaultdict(int)
            for row in cursor:
                try:
                    data = json.loads(row[1]) if isinstance(row[1], str) else row[1]
                    tool_name = data.get("tool_name", "Unknown")
                    tool_counts[tool_name] += 1
                except (json.JSONDecodeError, TypeError):
                    pass

            caps.tools_used = list(tool_counts.keys())
            caps.tool_frequency = dict(tool_counts)

            # Detect capabilities from tool usage
            if "WebSearch" in caps.tools_used or "WebFetch" in caps.tools_used:
                caps.web_access = True

        except Exception as e:
            logger.error(f"Error extracting capabilities: {e}")

        return caps

    def _count_events(self, session_id: str) -> int:
        """Count events for a session."""
        if not self.storage:
            return 0

        try:
            cursor = self.storage.sqlite.conn.execute(
                "SELECT COUNT(*) FROM events WHERE session_id = ?",
                (session_id,)
            )
            row = cursor.fetchone()
            return row[0] if row else 0
        except Exception:
            return 0

    def get_active_sessions(self) -> List[SessionMetadata]:
        """Get metadata for currently active sessions."""
        registry = self.load_instance_registry()
        active = []

        for session_id, entry in registry.items():
            if entry.get("status") == "active":
                metadata = self.aggregate_session(session_id)
                if metadata:
                    active.append(metadata)

        return active

    def get_agent_cards(self, limit: int = 50) -> List[AgentCard]:
        """
        Get A2A-compatible Agent Cards for sessions.

        Returns cards for the most recent/active sessions.
        """
        # Get recent sessions from store
        cards = []

        # First, try active sessions
        for metadata in self.get_active_sessions():
            cards.append(metadata.to_agent_card())
            if len(cards) >= limit:
                return cards

        # Then add from cache
        for metadata in self.store.search(limit=limit - len(cards)):
            card = metadata.to_agent_card()
            # Avoid duplicates by name
            if not any(c.name == card.name for c in cards):
                cards.append(card)

        return cards

    def get_session_by_name(self, name: str) -> Optional[SessionMetadata]:
        """Find a session by agent name."""
        # Check cache first
        for metadata in self.store._cache.values():
            if metadata.name and metadata.name.lower() == name.lower():
                return metadata

        # Search statusline events
        for short_id, events in self._statusline_cache.items():
            for event in events:
                if event.get("type") == "name" and event.get("value") == name:
                    return self.aggregate_session(short_id)

        return None

    def export_agent_cards_json(self, limit: int = 50) -> str:
        """Export Agent Cards as JSON for A2A compatibility."""
        cards = self.get_agent_cards(limit=limit)
        return json.dumps(
            [card.to_a2a_json() for card in cards],
            indent=2
        )

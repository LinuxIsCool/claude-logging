"""
Sync Manager

Coordinates the watcher and translator to provide real-time sync
between ~/.claude/ and the logging database.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from dataclasses import asdict

from .storage import StorageManager, Event
from .translator import EventTranslator
from .watcher import ClaudeWatcher, FileChange, ChangeType

logger = logging.getLogger(__name__)


class SyncManager:
    """
    Manages synchronization between Claude Code native data and logging database.

    Responsibilities:
    - Coordinate watcher and translator
    - Track sync positions per file
    - Handle incremental updates
    - Emit events to broadcast queue for live streaming
    """

    def __init__(
        self,
        claude_dir: Optional[Path] = None,
        storage: Optional[StorageManager] = None,
        broadcast_queue: Optional[asyncio.Queue] = None
    ):
        """
        Initialize the sync manager.

        Args:
            claude_dir: Path to ~/.claude/ (defaults to ~/.claude)
            storage: StorageManager instance
            broadcast_queue: Queue to publish events for SSE streaming
        """
        self.claude_dir = claude_dir or Path.home() / ".claude"
        self.storage = storage
        self.broadcast_queue = broadcast_queue

        self.translator = EventTranslator()
        self.watcher = ClaudeWatcher(
            claude_dir=self.claude_dir,
            callback=self._on_file_change
        )

        self._running = False
        self._pending_changes: asyncio.Queue = asyncio.Queue()
        self._process_task: Optional[asyncio.Task] = None
        self._last_sync_time: Optional[str] = None
        self._sync_stats: Dict[str, int] = {}
        self._sync_locks: Dict[str, asyncio.Lock] = {}  # Per-session locks

    @property
    def is_running(self) -> bool:
        """Check if sync manager is running."""
        return self._running

    @property
    def last_sync_time(self) -> Optional[str]:
        """Get timestamp of last sync operation."""
        return self._last_sync_time

    async def start(self) -> None:
        """Start the sync manager and watcher."""
        if self._running:
            return

        self._running = True

        # Start the change processor
        self._process_task = asyncio.create_task(self._process_changes())

        # Start the file watcher
        await self.watcher.start()

    async def stop(self) -> None:
        """Stop the sync manager and watcher."""
        self._running = False

        # Stop watcher first
        await self.watcher.stop()

        # Stop processor
        if self._process_task:
            self._process_task.cancel()
            try:
                await self._process_task
            except asyncio.CancelledError:
                pass
            self._process_task = None

    def _on_file_change(self, change: FileChange) -> None:
        """Callback from watcher when files change."""
        # Queue the change for async processing
        try:
            self._pending_changes.put_nowait(change)
        except asyncio.QueueFull:
            pass  # Drop if queue is full

    async def _process_changes(self) -> None:
        """Process queued file changes."""
        while self._running:
            try:
                # Wait for changes with timeout
                change = await asyncio.wait_for(
                    self._pending_changes.get(),
                    timeout=1.0
                )

                await self._handle_change(change)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error processing change: {e}", exc_info=True)

    async def _handle_change(self, change: FileChange) -> None:
        """Handle a single file change."""
        if change.change_type == ChangeType.DELETED:
            return  # We don't handle deletions yet

        if not change.session_id:
            return

        if not self.storage:
            return

        try:
            if change.is_subagent and change.agent_id:
                # Sync subagent transcript
                await self._sync_subagent(
                    change.path,
                    change.session_id,
                    change.agent_id
                )
            else:
                # Sync main transcript
                await self._sync_transcript(
                    change.path,
                    change.session_id
                )

            self._last_sync_time = datetime.now(timezone.utc).isoformat()

        except Exception as e:
            logger.error(f"Error handling change for {change.session_id}: {e}", exc_info=True)

    def _get_sync_lock(self, sync_key: str) -> asyncio.Lock:
        """Get or create a lock for a sync key."""
        if sync_key not in self._sync_locks:
            self._sync_locks[sync_key] = asyncio.Lock()
        return self._sync_locks[sync_key]

    async def _sync_transcript(
        self,
        transcript_path: Path,
        session_id: str
    ) -> int:
        """
        Sync a transcript file incrementally.

        Returns number of events synced.
        """
        if not transcript_path.exists():
            return 0

        sync_key = f"transcript:{session_id}"

        # Use per-session lock to prevent race conditions
        async with self._get_sync_lock(sync_key):
            last_position = self._get_sync_position(sync_key)
            current_size = transcript_path.stat().st_size

            if current_size <= last_position:
                return 0  # No new content

            # Parse new content
            events, new_position = self.translator.parse_transcript_file(
                transcript_path=transcript_path,
                session_id=session_id,
                start_position=last_position
            )

            # Store events
            synced = 0
            for event in events:
                try:
                    self._store_event(event)
                    synced += 1

                    # Broadcast for live streaming
                    if self.broadcast_queue:
                        await self.broadcast_queue.put(asdict(event))

                except Exception as e:
                    logger.error(f"Error storing event {event.id}: {e}")

            # Update sync position
            self._update_sync_position(sync_key, new_position, "transcript", str(transcript_path))

            # Update stats
            self._sync_stats[session_id] = self._sync_stats.get(session_id, 0) + synced

            if synced > 0:
                logger.debug(f"Synced {synced} events for session {session_id[:8]}")

            return synced

    async def _sync_subagent(
        self,
        transcript_path: Path,
        parent_session_id: str,
        agent_id: str
    ) -> int:
        """
        Sync a subagent transcript.

        Subagents are synced as separate sessions with parent reference.
        """
        if not transcript_path.exists():
            return 0

        sync_key = f"subagent:{parent_session_id}:{agent_id}"

        # Use per-session lock to prevent race conditions
        async with self._get_sync_lock(sync_key):
            last_position = self._get_sync_position(sync_key)
            current_size = transcript_path.stat().st_size

            if current_size <= last_position:
                return 0

            # For subagents, we translate the entire file and track position
            events, summary = self.translator.translate_subagent_transcript(
                transcript_path=transcript_path,
                parent_session_id=parent_session_id,
                agent_id=agent_id
            )

            # Only sync new events (deduplication via event ID)
            synced = 0
            for event in events:
                try:
                    # Check if event already exists (deduplication)
                    existing = self.storage.sqlite.conn.execute(
                        "SELECT 1 FROM events WHERE id = ?",
                        (event.id,)
                    ).fetchone()

                    if not existing:
                        self._store_event(event)
                        synced += 1

                        if self.broadcast_queue:
                            await self.broadcast_queue.put(asdict(event))

                except Exception as e:
                    logger.error(f"Error storing subagent event {event.id}: {e}")

            # Update sync position
            self._update_sync_position(sync_key, current_size, "subagent", str(transcript_path))

            if synced > 0:
                logger.debug(f"Synced {synced} subagent events for {agent_id[:8]}")

            return synced

    def _get_sync_position(self, sync_key: str) -> int:
        """Get last synced position for a file."""
        if not self.storage:
            return 0

        try:
            cursor = self.storage.sqlite.conn.execute(
                "SELECT last_position FROM sync_state WHERE session_id = ?",
                (sync_key,)
            )
            row = cursor.fetchone()
            return row[0] if row else 0
        except Exception:
            return 0

    def _update_sync_position(
        self,
        sync_key: str,
        position: int,
        source_type: str,
        source_path: str
    ) -> None:
        """Update sync position for a file."""
        if not self.storage:
            return

        try:
            self.storage.sqlite.conn.execute("""
                INSERT OR REPLACE INTO sync_state
                (session_id, last_position, last_sync)
                VALUES (?, ?, ?)
            """, (sync_key, position, datetime.now(timezone.utc).isoformat()))
            self.storage.sqlite.conn.commit()
        except Exception:
            pass

    def _store_event(self, event: Event) -> None:
        """Store an event in the database."""
        if not self.storage:
            return

        try:
            self.storage.sqlite.insert_event(event)
        except Exception:
            pass

    async def sync_session(self, session_id: str) -> int:
        """
        Manually sync a specific session.

        Searches for the session transcript and syncs it.
        """
        if not self.storage:
            return 0

        # Find the session
        sessions = self.watcher.discover_sessions()
        session_info = next(
            (s for s in sessions if s["session_id"] == session_id),
            None
        )

        if not session_info:
            return 0

        synced = 0

        # Sync main transcript
        synced += await self._sync_transcript(
            session_info["transcript_path"],
            session_id
        )

        # Sync subagents
        for sa in session_info.get("subagents", []):
            synced += await self._sync_subagent(
                sa["path"],
                session_id,
                sa["agent_id"]
            )

        return synced

    async def sync_all_historical(self) -> Dict[str, int]:
        """
        Import all historical sessions from ~/.claude/.

        Returns dict mapping session_id to events synced.
        """
        results = {}

        if not self.storage:
            return results

        sessions = self.watcher.discover_sessions()

        for session_info in sessions:
            session_id = session_info["session_id"]

            try:
                # Sync main transcript
                count = await self._sync_transcript(
                    session_info["transcript_path"],
                    session_id
                )

                # Sync subagents
                for sa in session_info.get("subagents", []):
                    count += await self._sync_subagent(
                        sa["path"],
                        session_id,
                        sa["agent_id"]
                    )

                results[session_id] = count

            except Exception:
                results[session_id] = 0

        self._last_sync_time = datetime.now(timezone.utc).isoformat()
        return results

    def get_status(self) -> Dict[str, Any]:
        """Get current sync status."""
        sessions = self.watcher.discover_sessions() if self.watcher else []

        return {
            "running": self._running,
            "watcher_active": self.watcher.is_running if self.watcher else False,
            "discovered_sessions": len(sessions),
            "last_sync": self._last_sync_time,
            "sync_stats": self._sync_stats,
            "pending_changes": self._pending_changes.qsize(),
        }

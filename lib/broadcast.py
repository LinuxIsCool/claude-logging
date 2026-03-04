"""
Broadcast Service

Manages Server-Sent Events (SSE) connections for real-time event streaming.
Distributes events to subscribers with optional filtering.
"""

import asyncio
import logging
from typing import Optional, Dict, Any, List, Set
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Subscriber:
    """A connected SSE subscriber."""
    queue: asyncio.Queue
    filters: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    _id: int = field(default_factory=lambda: id(object()))  # Unique ID for hashing

    def __hash__(self):
        return self._id

    def __eq__(self, other):
        if isinstance(other, Subscriber):
            return self._id == other._id
        return False

    def matches(self, event: Dict[str, Any]) -> bool:
        """Check if an event matches this subscriber's filters."""
        if not self.filters:
            return True

        # Filter by session_id
        if "session_id" in self.filters:
            if event.get("session_id") != self.filters["session_id"]:
                return False

        # Filter by event types
        if "event_types" in self.filters:
            allowed_types = self.filters["event_types"]
            if event.get("type") not in allowed_types:
                return False

        return True


class BroadcastService:
    """
    Manages SSE subscribers and broadcasts events.

    Usage:
        broadcast = BroadcastService()
        await broadcast.start()

        # Subscribe with filters
        queue = broadcast.subscribe({"session_id": "abc123"})

        # Publish events (from sync manager)
        await broadcast.publish({"type": "UserPromptSubmit", ...})

        # Unsubscribe when done
        broadcast.unsubscribe(queue)
    """

    def __init__(self):
        self._subscribers: Set[Subscriber] = set()
        self._queue_to_sub: Dict[asyncio.Queue, Subscriber] = {}  # Fast lookup
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()  # Protects subscriber set operations

    @property
    def queue(self) -> asyncio.Queue:
        """Get the input queue for publishing events."""
        return self._queue

    @property
    def subscriber_count(self) -> int:
        """Get number of active subscribers."""
        return len(self._subscribers)

    async def start(self) -> None:
        """Start the broadcast worker."""
        if self._running:
            return

        self._running = True
        self._worker_task = asyncio.create_task(self._broadcast_worker())

    async def stop(self) -> None:
        """Stop the broadcast worker."""
        self._running = False

        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

        # Clear all subscribers
        async with self._lock:
            self._subscribers.clear()
            self._queue_to_sub.clear()

    async def subscribe(self, filters: Optional[Dict[str, Any]] = None) -> asyncio.Queue:
        """
        Subscribe to event broadcasts.

        Args:
            filters: Optional dict with:
                - session_id: Only events for this session
                - event_types: List of event types to receive

        Returns:
            Queue that will receive matching events
        """
        from datetime import datetime, timezone

        subscriber = Subscriber(
            queue=asyncio.Queue(maxsize=100),
            filters=filters or {},
            created_at=datetime.now(timezone.utc).isoformat()
        )

        async with self._lock:
            self._subscribers.add(subscriber)
            self._queue_to_sub[subscriber.queue] = subscriber

        logger.debug(f"New subscriber added, total: {len(self._subscribers)}")
        return subscriber.queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        """
        Unsubscribe from event broadcasts.

        Args:
            queue: The queue returned from subscribe()
        """
        async with self._lock:
            subscriber = self._queue_to_sub.pop(queue, None)
            if subscriber:
                self._subscribers.discard(subscriber)
                logger.debug(f"Subscriber removed, remaining: {len(self._subscribers)}")

    async def publish(self, event: Dict[str, Any]) -> int:
        """
        Publish an event to all matching subscribers.

        Args:
            event: Event dict to broadcast

        Returns:
            Number of subscribers that received the event
        """
        await self._queue.put(event)
        return len(self._subscribers)

    async def _broadcast_worker(self) -> None:
        """Worker that distributes events to subscribers."""
        while self._running:
            try:
                # Wait for events with timeout
                event = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=1.0
                )

                # Broadcast to matching subscribers
                await self._distribute(event)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _distribute(self, event: Dict[str, Any]) -> None:
        """Distribute an event to all matching subscribers."""
        dead_subscribers = []

        # Take a snapshot of subscribers under lock
        async with self._lock:
            subscribers_snapshot = list(self._subscribers)

        for subscriber in subscribers_snapshot:
            if not subscriber.matches(event):
                continue

            try:
                # Non-blocking put
                subscriber.queue.put_nowait(event)
            except asyncio.QueueFull:
                # Subscriber queue is full - they're not keeping up
                # Mark for removal
                dead_subscribers.append(subscriber)
                logger.warning(f"Subscriber queue full, removing")
            except Exception as e:
                logger.error(f"Error distributing to subscriber: {e}")

        # Remove dead subscribers under lock
        if dead_subscribers:
            async with self._lock:
                for subscriber in dead_subscribers:
                    self._subscribers.discard(subscriber)
                    self._queue_to_sub.pop(subscriber.queue, None)

    def get_status(self) -> Dict[str, Any]:
        """Get broadcast service status."""
        return {
            "running": self._running,
            "subscriber_count": len(self._subscribers),
            "queue_size": self._queue.qsize(),
            "subscribers": [
                {
                    "created_at": s.created_at,
                    "filters": s.filters,
                    "queue_size": s.queue.qsize(),
                }
                for s in self._subscribers
            ]
        }

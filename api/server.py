"""
FastAPI Server for Logging Plugin

Provides REST API for search, statistics, and real-time updates.
Includes live sync with ~/.claude/ directory for real-time event streaming.
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from pathlib import Path
import asyncio
import json
import os
import mimetypes
import re

# Add parent to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.storage import StorageManager
from lib.search import SearchService
from lib.embeddings import EmbeddingService, EmbeddingStorage
from lib.sync_manager import SyncManager
from lib.broadcast import BroadcastService
from lib.session_metadata import SessionMetadata, AgentCard, SessionStatus
from lib.metadata_aggregator import MetadataAggregator


# Configuration — centralized storage under ~/.claude/local/logging/<encoded-project-dir>
_project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
_encoded = _project_dir.replace("/", "-")
STORAGE_PATH = Path.home() / ".claude" / "local" / "logging" / _encoded


class EmbeddingManager:
    """
    Combines EmbeddingService (encode) and EmbeddingStorage (search) into a single
    interface expected by SearchService.semantic_search().
    """
    def __init__(self, storage_path: Path):
        self.service = EmbeddingService()
        self.storage = EmbeddingStorage(storage_path / "embeddings.db")
        self._available = self.service.is_available

    @property
    def is_available(self) -> bool:
        return self._available

    def encode(self, texts):
        """Encode texts using the embedding service."""
        return self.service.encode(texts)

    def search(self, query_embedding, limit=20, filters=None):
        """Search for similar embeddings using the storage."""
        return self.storage.search(query_embedding, limit=limit, filters=filters)


# Initialize services
storage = StorageManager(STORAGE_PATH)

# Initialize embedding manager (combines service + storage for SearchService)
embedding_manager = EmbeddingManager(STORAGE_PATH)
if embedding_manager.is_available:
    print(f"✓ Embeddings available (model: {embedding_manager.service.model_name})")
else:
    print("⚠ Embeddings not available (sentence-transformers not installed)")

search = SearchService(storage.sqlite, embedding_service=embedding_manager if embedding_manager.is_available else None)

# Initialize live sync components
CLAUDE_DIR = Path.home() / ".claude"
broadcast = BroadcastService()
sync_manager = SyncManager(
    claude_dir=CLAUDE_DIR,
    storage=storage,
    broadcast_queue=broadcast.queue
)

# Initialize metadata aggregator
metadata_aggregator = MetadataAggregator(
    claude_dir=CLAUDE_DIR,
    storage=storage
)

# Create FastAPI app
app = FastAPI(
    title="Claude Logging API",
    description="Search and explore Claude Code conversation history",
    version="1.0.0"
)

# CORS for web interface
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Request/Response models
class SearchRequest(BaseModel):
    query: str
    limit: int = 20
    event_types: Optional[List[str]] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    use_semantic: bool = False


class SearchResultItem(BaseModel):
    event_id: str
    session_id: str
    event_type: str
    content: str
    score: float  # RRF score for ranking
    timestamp: str
    source: str
    cosine_similarity: float = 0.0  # Semantic similarity (0.0-1.0) for display


class SearchResponse(BaseModel):
    results: List[SearchResultItem]
    total: int
    time_ms: float


class SessionSummary(BaseModel):
    id: str
    started_at: str
    ended_at: Optional[str]
    cwd: Optional[str]
    summary: Optional[str]
    event_count: int
    event_type_counts: Optional[dict] = None  # Counts by event type


class StatsResponse(BaseModel):
    session_count: int
    event_count: int
    total_tokens: int
    first_session: Optional[str]
    last_session: Optional[str]


# Routes
@app.get("/")
async def root():
    """API root."""
    return {"status": "ok", "service": "claude-logging-api"}


@app.post("/api/search", response_model=SearchResponse)
async def search_logs(request: SearchRequest):
    """
    Search conversation history.

    Uses hybrid search (FTS5 + optional semantic) with RRF fusion.
    """
    try:
        # Sync any new events first
        storage.sync_all()

        # Perform search
        results, time_ms = search.hybrid_search(
            query=request.query,
            limit=request.limit,
            event_types=request.event_types,
            date_from=request.date_from,
            date_to=request.date_to,
            use_semantic=request.use_semantic
        )

        return SearchResponse(
            results=[
                SearchResultItem(
                    event_id=r.event_id,
                    session_id=r.session_id,
                    event_type=r.event_type,
                    # NOTE: Content truncated to 500 chars for API response size.
                    # Full content available via session detail endpoint.
                    # Review: Is this limit appropriate? Consider making configurable.
                    content=r.content[:500],
                    score=r.score,
                    timestamp=r.timestamp,
                    source=r.source,
                    cosine_similarity=r.cosine_similarity
                )
                for r in results
            ],
            total=len(results),
            time_ms=time_ms
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sessions", response_model=List[SessionSummary])
async def list_sessions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
):
    """List sessions with pagination."""
    try:
        storage.sync_all()
        sessions = storage.sqlite.list_sessions(
            limit=limit,
            offset=offset,
            date_from=date_from,
            date_to=date_to
        )

        # Get event type counts for all sessions in batch
        session_ids = [s["id"] for s in sessions]
        type_counts = storage.sqlite.get_event_type_counts_batch(session_ids)

        return [
            SessionSummary(
                id=s["id"],
                started_at=s["started_at"],
                ended_at=s.get("ended_at"),
                cwd=s.get("cwd"),
                summary=s.get("summary"),
                event_count=s.get("event_count", 0),
                event_type_counts=type_counts.get(s["id"], {})
            )
            for s in sessions
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    """Get a specific session with all events."""
    try:
        session = storage.sqlite.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # Get events from JSONL
        events = list(storage.jsonl.read_session(session_id))

        return {
            "session": session,
            "events": events
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stats", response_model=StatsResponse)
async def get_stats():
    """Get overall statistics."""
    try:
        storage.sync_all()
        stats = storage.sqlite.get_stats()

        return StatsResponse(
            session_count=stats.get("session_count", 0) or 0,
            event_count=stats.get("event_count", 0) or 0,
            total_tokens=stats.get("total_tokens", 0) or 0,
            first_session=stats.get("first_session"),
            last_session=stats.get("last_session")
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/subagent-transcript/{session_id}/{agent_id}")
async def get_subagent_transcript(session_id: str, agent_id: str):
    """
    Get a subagent's transcript content.

    Returns the prompt (first message) and response (last assistant message).
    """
    try:
        # Find the subagent transcript file
        # Path pattern: ~/.claude/projects/.../session_id/subagents/agent-{agent_id}.jsonl
        claude_dir = Path.home() / ".claude" / "projects"

        # Search for the subagent file
        subagent_file = None
        for project_dir in claude_dir.glob("*"):
            candidate = project_dir / session_id / "subagents" / f"agent-{agent_id}.jsonl"
            if candidate.exists():
                subagent_file = candidate
                break

        if not subagent_file:
            raise HTTPException(status_code=404, detail="Subagent transcript not found")

        # Read the transcript
        messages = []
        with open(subagent_file, "r") as f:
            for line in f:
                if line.strip():
                    try:
                        entry = json.loads(line)
                        msg_type = entry.get("type")

                        if msg_type == "user":
                            # First user message is the prompt
                            content = entry.get("message", {}).get("content", "")
                            if isinstance(content, str):
                                messages.append({"type": "prompt", "content": content})
                            elif isinstance(content, list) and len(content) > 0:
                                # Content is a list of content blocks
                                text = content[0].get("text", "") if isinstance(content[0], dict) else str(content[0])
                                messages.append({"type": "prompt", "content": text})

                        elif msg_type == "assistant":
                            # Get assistant text content
                            msg_content = entry.get("message", {}).get("content", [])
                            if isinstance(msg_content, str):
                                messages.append({"type": "response", "content": msg_content})
                            elif isinstance(msg_content, list):
                                for block in msg_content:
                                    if isinstance(block, dict) and block.get("type") == "text":
                                        messages.append({"type": "response", "content": block.get("text", "")})

                    except json.JSONDecodeError:
                        continue

        # Get the prompt (first message) and final response (last text response)
        prompt = ""
        final_response = ""

        for msg in messages:
            if msg["type"] == "prompt" and not prompt:
                prompt = msg["content"]
            elif msg["type"] == "response":
                final_response = msg["content"]  # Keep updating to get the last one

        return {
            "agent_id": agent_id,
            "session_id": session_id,
            "prompt": prompt,
            "response": final_response,
            "message_count": len(messages)
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/events/recent")
async def get_recent_events(
    limit: int = Query(50, ge=1, le=200),
    event_types: Optional[str] = None
):
    """Get recent events (for browsing without search)."""
    try:
        # Sync first to ensure we have latest
        storage.sync_all()

        # Build query
        sql = """
            SELECT id, session_id, type, ts, content
            FROM events
            WHERE content IS NOT NULL AND content != ''
        """
        params = []

        if event_types:
            types = event_types.split(",")
            placeholders = ",".join("?" * len(types))
            sql += f" AND type IN ({placeholders})"
            params.extend(types)

        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)

        cursor = storage.sqlite.conn.execute(sql, params)
        results = []
        for row in cursor:
            results.append({
                "event_id": row[0],
                "session_id": row[1],
                "event_type": row[2],
                "timestamp": row[3],
                "content": row[4] or "",
                "score": 0,
                "source": "recent"
            })

        return {"results": results, "total": len(results), "time_ms": 0}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sync")
async def sync_all():
    """Sync all JSONL files to SQLite."""
    try:
        events_synced = storage.sync_all()
        return {"synced": events_synced}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/images/{session_id}/{filename}")
async def serve_image(session_id: str, filename: str):
    """
    Serve image files extracted from user prompts.

    Images are stored when users paste/attach images to their prompts.
    This endpoint serves those images for display in the web UI.

    Checks multiple storage locations to support different plugin versions.
    """
    try:
        # Security: validate session_id and filename format
        # Only allow alphanumeric, hyphens, underscores, and dots
        if not re.match(r'^[a-zA-Z0-9\-]+$', session_id):
            raise HTTPException(status_code=400, detail="Invalid session ID format")
        if not re.match(r'^[a-zA-Z0-9_\-\.]+$', filename):
            raise HTTPException(status_code=400, detail="Invalid filename format")

        # Prevent path traversal
        if ".." in filename or "/" in filename or "\\" in filename:
            raise HTTPException(status_code=400, detail="Invalid filename")

        # Validate file extension is an allowed image type
        allowed_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
        file_ext = filename.lower().rsplit('.', 1)[-1] if '.' in filename else ''
        if f'.{file_ext}' not in allowed_extensions:
            raise HTTPException(status_code=400, detail="Invalid file type")

        # Check multiple possible image locations
        # 1. New plugin path: .claude/local/logging/images/{session_id}/
        # 2. Old plugin path: .claude/logging/YYYY/MM/images/{session_id}/
        project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
        possible_paths = [
            STORAGE_PATH / "images" / session_id / filename,
        ]

        # Also check the old plugin's date-based structure
        old_logging_dir = project_dir / ".claude" / "logging"
        if old_logging_dir.exists():
            # Search for images directory in any date folder
            for year_dir in old_logging_dir.glob("20*"):
                for month_dir in year_dir.glob("*"):
                    candidate = month_dir / "images" / session_id / filename
                    if candidate.exists():
                        possible_paths.insert(0, candidate)  # Prefer found paths

        # Find first existing path
        image_path = None
        for path in possible_paths:
            if path.exists():
                image_path = path
                break

        if not image_path:
            raise HTTPException(status_code=404, detail="Image not found")

        # Security: verify path is within an allowed directory
        allowed_roots = [STORAGE_PATH.resolve(), (project_dir / ".claude").resolve()]
        path_ok = False
        for root in allowed_roots:
            try:
                image_path.resolve().relative_to(root)
                path_ok = True
                break
            except ValueError:
                continue

        if not path_ok:
            raise HTTPException(status_code=403, detail="Access denied")

        # Determine content type
        content_type, _ = mimetypes.guess_type(str(image_path))
        if not content_type:
            content_type = "application/octet-stream"

        return FileResponse(
            image_path,
            media_type=content_type,
            filename=filename
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/events/stream")
async def stream_events(
    session_id: Optional[str] = None,
    event_types: Optional[str] = None
):
    """
    Stream new events using Server-Sent Events (SSE).

    Supports both hook-generated events and live sync from ~/.claude/.

    Query params:
        session_id: Filter to specific session
        event_types: Comma-separated list of event types to include
    """
    # Build filters
    filters = {}
    if session_id:
        filters["session_id"] = session_id
    if event_types:
        filters["event_types"] = event_types.split(",")

    # Subscribe to broadcast (async)
    subscriber_queue = await broadcast.subscribe(filters)

    async def event_generator():
        try:
            while True:
                try:
                    # Wait for events with timeout
                    event = await asyncio.wait_for(
                        subscriber_queue.get(),
                        timeout=30.0
                    )
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive
                    yield f": keepalive\n\n"
        except Exception:
            pass
        finally:
            await broadcast.unsubscribe(subscriber_queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream"
    )


# ============================================================================
# Sync Endpoints
# ============================================================================

@app.get("/api/sync/status")
async def get_sync_status():
    """Get current sync status including watcher and discovered sessions."""
    return sync_manager.get_status()


@app.post("/api/sync/session/{session_id}")
async def sync_session(session_id: str):
    """
    Manually sync a specific session from ~/.claude/.

    Imports new events from the native transcript.
    """
    try:
        count = await sync_manager.sync_session(session_id)
        return {"session_id": session_id, "events_synced": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sync/historical")
async def sync_historical():
    """
    Import all historical sessions from ~/.claude/.

    This may take a while for large transcript collections.
    """
    try:
        results = await sync_manager.sync_all_historical()
        return {
            "sessions_processed": len(results),
            "total_events": sum(results.values()),
            "by_session": results
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sync/discover")
async def discover_sessions():
    """
    Discover all sessions in ~/.claude/ without syncing.

    Returns session info including size, subagent count, etc.
    """
    try:
        sessions = sync_manager.watcher.discover_sessions()
        return {
            "count": len(sessions),
            "sessions": [
                {
                    "session_id": s["session_id"],
                    "project_path": s["project_path"],
                    "size_bytes": s["size"],
                    "subagent_count": len(s.get("subagents", [])),
                }
                for s in sessions
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/broadcast/status")
async def get_broadcast_status():
    """Get broadcast service status including subscriber count."""
    return broadcast.get_status()


# ============================================================================
# Session Metadata Endpoints
# ============================================================================

@app.get("/api/metadata/sessions")
async def get_session_metadata(
    limit: int = Query(50, ge=1, le=200),
    status: Optional[str] = None,
    name: Optional[str] = None
):
    """
    Get enriched session metadata.

    Aggregates data from statusline, registry, and logging events.
    """
    try:
        # Load latest statusline events
        metadata_aggregator.load_statusline_events()

        # Aggregate all sessions
        all_metadata = metadata_aggregator.aggregate_all()

        # Filter
        results = []
        for m in all_metadata:
            if status and m.task_status.value != status:
                continue
            if name and name.lower() not in (m.name or "").lower():
                continue
            results.append(m)

            if len(results) >= limit:
                break

        # Convert to dict for JSON response
        return {
            "count": len(results),
            "sessions": [
                {
                    "session_id": m.session_id,
                    "name": m.name,
                    "description": m.description,
                    "summary": m.summary,
                    "status": m.task_status.value,
                    "model": m.model_display_name,
                    "cwd": m.cwd,
                    "started_at": m.started_at,
                    "last_activity": m.last_activity,
                    "prompt_count": m.prompt_count,
                    "event_count": m.event_count,
                    "cost_usd": m.cost.total_cost_usd,
                    "context_pct": m.cost.context_percentage,
                    "git_branch": m.git.branch,
                    "process_number": m.process_number,
                    "auto_named": m.auto_named,
                }
                for m in results
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/metadata/sessions/{session_id}")
async def get_session_metadata_detail(session_id: str):
    """Get detailed metadata for a specific session."""
    try:
        metadata_aggregator.load_statusline_events()
        metadata = metadata_aggregator.aggregate_session(session_id)

        if not metadata:
            raise HTTPException(status_code=404, detail="Session not found")

        return {
            "session_id": metadata.session_id,
            "name": metadata.name,
            "description": metadata.description,
            "summary": metadata.summary,
            "status": metadata.task_status.value,
            "room_type": metadata.room_type.value,
            "parent_session_id": metadata.parent_session_id,
            "model": metadata.model,
            "model_display_name": metadata.model_display_name,
            "claude_code_version": metadata.claude_code_version,
            "cwd": metadata.cwd,
            "world_id": metadata.world_id,
            "started_at": metadata.started_at,
            "ended_at": metadata.ended_at,
            "last_activity": metadata.last_activity,
            "duration_seconds": metadata.duration_seconds,
            "prompt_count": metadata.prompt_count,
            "event_count": metadata.event_count,
            "agent_session_num": metadata.agent_session_num,
            "cost": {
                "total_usd": metadata.cost.total_cost_usd,
                "input_tokens": metadata.cost.input_tokens,
                "output_tokens": metadata.cost.output_tokens,
                "cache_read_tokens": metadata.cost.cache_read_tokens,
                "cache_write_tokens": metadata.cost.cache_write_tokens,
                "context_pct": metadata.cost.context_percentage,
                "peak_context_pct": metadata.cost.peak_context_percentage,
            },
            "git": {
                "branch": metadata.git.branch,
                "lines_added": metadata.git.lines_added,
                "lines_removed": metadata.git.lines_removed,
                "is_dirty": metadata.git.is_dirty,
            },
            "capabilities": {
                "tools_used": metadata.capabilities.tools_used,
                "tool_frequency": metadata.capabilities.tool_frequency,
                "web_access": metadata.capabilities.web_access,
            },
            "process_number": metadata.process_number,
            "pane_id": metadata.pane_id,
            "tags": metadata.tags,
            "auto_named": metadata.auto_named,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/metadata/active")
async def get_active_sessions():
    """Get metadata for currently active sessions from registry."""
    try:
        active = metadata_aggregator.get_active_sessions()
        return {
            "count": len(active),
            "sessions": [
                {
                    "session_id": m.session_id,
                    "name": m.name,
                    "description": m.description,
                    "summary": m.summary,
                    "model": m.model_display_name,
                    "cwd": m.cwd,
                    "cost_usd": m.cost.total_cost_usd,
                    "context_pct": m.cost.context_percentage,
                    "prompt_count": m.prompt_count,
                    "process_number": m.process_number,
                }
                for m in active
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# A2A Agent Card Endpoints
# ============================================================================

@app.get("/api/agents")
async def get_agent_cards(limit: int = Query(50, ge=1, le=200)):
    """
    Get A2A-compatible Agent Cards for sessions.

    These can be used for agent discovery and capability advertisement.
    See: https://a2a-protocol.org/latest/specification/
    """
    try:
        metadata_aggregator.load_statusline_events()
        cards = metadata_aggregator.get_agent_cards(limit=limit)

        return {
            "count": len(cards),
            "agents": [card.to_a2a_json() for card in cards]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/agents/{name}")
async def get_agent_by_name(name: str):
    """
    Get Agent Card for a specific agent by name.

    Names are auto-generated identities like 'Archivist', 'Spark', etc.
    """
    try:
        metadata_aggregator.load_statusline_events()
        metadata = metadata_aggregator.get_session_by_name(name)

        if not metadata:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        card = metadata.to_agent_card()
        return card.to_a2a_json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/agents/export")
async def export_agent_cards(limit: int = Query(50, ge=1, le=200)):
    """
    Export Agent Cards as A2A-compatible JSON.

    Returns raw JSON suitable for agent registries.
    """
    try:
        metadata_aggregator.load_statusline_events()
        json_str = metadata_aggregator.export_agent_cards_json(limit=limit)
        return json.loads(json_str)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.on_event("startup")
async def startup():
    """Initialize services on startup."""
    # Sync hook-generated events from JSONL to SQLite
    storage.sync_all()

    # Start broadcast service for SSE
    await broadcast.start()
    print("✓ Broadcast service started")

    # Start live sync with ~/.claude/
    await sync_manager.start()
    print(f"✓ Live sync started (watching {CLAUDE_DIR})")

    # Report discovered sessions
    sessions = sync_manager.watcher.discover_sessions()
    print(f"✓ Discovered {len(sessions)} sessions in ~/.claude/")


@app.on_event("shutdown")
async def shutdown():
    """Clean up on shutdown."""
    # Stop sync manager first
    await sync_manager.stop()

    # Stop broadcast service
    await broadcast.stop()

    # Close storage
    storage.close()


def main():
    """Run the server."""
    import uvicorn

    port = int(os.environ.get("LOGGING_API_PORT", 3001))

    uvicorn.run(
        "api.server:app",
        host="127.0.0.1",
        port=port,
        reload=True,
        log_level="info"
    )


if __name__ == "__main__":
    main()

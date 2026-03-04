"""
FastAPI Server for Logging Plugin

Provides REST API for search, statistics, and real-time updates.
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from typing import Optional, List
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


# Configuration
STORAGE_PATH = Path(os.environ.get(
    "LOGGING_STORAGE_PATH",
    os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()), ".claude/local/logging")
))

# Initialize services
storage = StorageManager(STORAGE_PATH)
search = SearchService(storage.sqlite)

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
    score: float
    timestamp: str
    source: str


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
                    content=r.content[:500],  # Truncate for response
                    score=r.score,
                    timestamp=r.timestamp,
                    source=r.source
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
async def stream_events():
    """
    Stream new events using Server-Sent Events (SSE).

    Watches the sessions directory for changes and emits events.
    """
    async def event_generator():
        try:
            import watchfiles

            sessions_dir = STORAGE_PATH / "sessions"

            async for changes in watchfiles.awatch(sessions_dir):
                for change_type, path in changes:
                    if path.endswith(".jsonl"):
                        # Read last line of changed file
                        try:
                            with open(path, "r") as f:
                                lines = f.readlines()
                                if lines:
                                    event = json.loads(lines[-1])
                                    yield f"data: {json.dumps(event)}\n\n"
                        except Exception:
                            pass
        except ImportError:
            # watchfiles not installed, poll instead
            seen_positions = {}

            while True:
                sessions_dir = STORAGE_PATH / "sessions"

                for session_file in sessions_dir.glob("*.jsonl"):
                    current_size = session_file.stat().st_size
                    last_size = seen_positions.get(str(session_file), 0)

                    if current_size > last_size:
                        with open(session_file, "r") as f:
                            f.seek(last_size)
                            for line in f:
                                if line.strip():
                                    yield f"data: {line}\n\n"

                        seen_positions[str(session_file)] = current_size

                await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream"
    )


@app.on_event("startup")
async def startup():
    """Sync all sessions on startup."""
    storage.sync_all()


@app.on_event("shutdown")
async def shutdown():
    """Clean up on shutdown."""
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

"""
Storage Layer for Logging Plugin

Provides both JSONL (source of truth) and SQLite (indexed search) storage.
"""

import sqlite3
import json
import sys
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Iterator, List
from dataclasses import dataclass, asdict, fields as dc_fields

# Cross-platform file locking
if sys.platform == "win32":
    class _NoOpFcntl:
        """No-op file locking on Windows. See README for platform notes."""
        LOCK_EX = 0
        LOCK_UN = 0
        @staticmethod
        def flock(fd, op):
            pass
    fcntl = _NoOpFcntl()
else:
    import fcntl


@dataclass
class Session:
    """Session metadata."""
    id: str
    started_at: str
    ended_at: Optional[str] = None
    cwd: Optional[str] = None
    summary: Optional[str] = None
    tags: List[str] = None
    event_count: int = 0
    total_tokens: int = 0

    def __post_init__(self):
        if self.tags is None:
            self.tags = []


@dataclass
class Event:
    """Event record."""
    id: str
    session_id: str
    type: str
    ts: str
    agent_session_num: int = 0
    data: dict = None
    content: Optional[str] = None
    images: Optional[list] = None  # Image references for UserPromptSubmit events

    def __post_init__(self):
        if self.data is None:
            self.data = {}


# Pre-compute known field names for schema-safe construction
_EVENT_FIELDS = {f.name for f in dc_fields(Event)}


class JSONLStorage:
    """Append-only JSONL storage (source of truth)."""

    def __init__(self, base_path: Path):
        self.base_path = base_path
        self.sessions_dir = base_path / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def get_session_path(self, session_id: str) -> Path:
        """Get path to session JSONL file."""
        return self.sessions_dir / f"{session_id}.jsonl"

    def append_event(self, event: Event) -> None:
        """Append event to session JSONL file with locking."""
        path = self.get_session_path(event.session_id)

        with open(path, "a") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def read_session(self, session_id: str) -> Iterator[dict]:
        """Read all events for a session."""
        path = self.get_session_path(session_id)
        if not path.exists():
            return

        with open(path, "r") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)

    def list_sessions(self) -> List[str]:
        """List all session IDs."""
        return [p.stem for p in self.sessions_dir.glob("*.jsonl")]

    def get_last_position(self, session_id: str) -> int:
        """Get file size (position) for incremental sync."""
        path = self.get_session_path(session_id)
        if path.exists():
            return path.stat().st_size
        return 0


class SQLiteStorage:
    """SQLite storage with FTS5 for search."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._write_lock = threading.Lock()
        self._init_schema()

    def _init_schema(self):
        """Initialize database schema."""
        self.conn.executescript("""
            -- Sessions table
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                started_at TIMESTAMP NOT NULL,
                ended_at TIMESTAMP,
                cwd TEXT,
                summary TEXT,
                tags JSON DEFAULT '[]',
                event_count INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_date
            ON sessions(started_at DESC);

            -- Events table
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                type TEXT NOT NULL,
                ts TIMESTAMP NOT NULL,
                agent_session_num INTEGER DEFAULT 0,
                data JSON NOT NULL,
                content TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );

            CREATE INDEX IF NOT EXISTS idx_events_session
            ON events(session_id);

            CREATE INDEX IF NOT EXISTS idx_events_type
            ON events(type);

            CREATE INDEX IF NOT EXISTS idx_events_ts
            ON events(ts DESC);

            -- FTS5 for full-text search
            CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
                event_id,
                session_id,
                type,
                content,
                tokenize='porter'
            );

            -- Sync state for JSONL → SQLite
            CREATE TABLE IF NOT EXISTS sync_state (
                session_id TEXT PRIMARY KEY,
                last_position INTEGER DEFAULT 0,
                last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Daily indices
            CREATE TABLE IF NOT EXISTS daily_indices (
                date DATE PRIMARY KEY,
                session_count INTEGER DEFAULT 0,
                event_count INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                summary TEXT,
                tags JSON DEFAULT '[]'
            );

            -- Session entity staging (Phase 2: session knowledge capture)
            CREATE TABLE IF NOT EXISTS session_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                entity_name TEXT NOT NULL,
                entity_type TEXT DEFAULT 'unknown',
                mention_count INTEGER DEFAULT 1,
                first_seen TIMESTAMP NOT NULL,
                context TEXT,
                UNIQUE(session_id, entity_name)
            );

            CREATE INDEX IF NOT EXISTS idx_session_entities_session
            ON session_entities(session_id);

            CREATE INDEX IF NOT EXISTS idx_session_entities_name
            ON session_entities(entity_name);

            CREATE INDEX IF NOT EXISTS idx_session_entities_type
            ON session_entities(entity_type);

            -- Session summaries (Phase 2: PostCompact → summary → KOI)
            CREATE TABLE IF NOT EXISTS session_summaries (
                session_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                source TEXT DEFAULT 'compact',
                entities_extracted INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );
        """)
        self.conn.commit()

    def insert_session(self, session: Session) -> None:
        """Insert or update a session."""
        with self._write_lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO sessions
                (id, started_at, ended_at, cwd, summary, tags, event_count, total_tokens)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session.id,
                session.started_at,
                session.ended_at,
                session.cwd,
                session.summary,
                json.dumps(session.tags),
                session.event_count,
                session.total_tokens,
            ))
            self.conn.commit()

    def insert_event(self, event: Event) -> None:
        """Insert event and update FTS index."""
        with self._write_lock:
            # Insert event
            self.conn.execute("""
                INSERT OR REPLACE INTO events
                (id, session_id, type, ts, agent_session_num, data, content)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                event.id,
                event.session_id,
                event.type,
                event.ts,
                event.agent_session_num,
                json.dumps(event.data),
                event.content,
            ))

            # Insert into FTS if there's content
            if event.content:
                self.conn.execute("""
                    INSERT OR REPLACE INTO events_fts
                    (event_id, session_id, type, content)
                    VALUES (?, ?, ?, ?)
                """, (event.id, event.session_id, event.type, event.content))

            self.conn.commit()

    def search(self, query: str, limit: int = 20) -> List[dict]:
        """Full-text search across events."""
        cursor = self.conn.execute("""
            SELECT
                e.id,
                e.session_id,
                e.type,
                e.ts,
                e.content,
                bm25(events_fts) as score
            FROM events_fts
            JOIN events e ON events_fts.event_id = e.id
            WHERE events_fts MATCH ?
            ORDER BY score
            LIMIT ?
        """, (query, limit))

        return [dict(row) for row in cursor]

    def get_session(self, session_id: str) -> Optional[dict]:
        """Get session by ID."""
        cursor = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def list_sessions(
        self,
        limit: int = 50,
        offset: int = 0,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None
    ) -> List[dict]:
        """List sessions with pagination and optional filtering."""
        sql = "SELECT * FROM sessions"
        params = []

        conditions = []
        if date_from:
            conditions.append("started_at >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("started_at <= ?")
            params.append(date_to)

        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        sql += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = self.conn.execute(sql, params)
        return [dict(row) for row in cursor]

    def get_event_type_counts(self, session_id: str) -> dict:
        """Get event counts by type for a session."""
        cursor = self.conn.execute("""
            SELECT type, COUNT(*) as count
            FROM events
            WHERE session_id = ?
            GROUP BY type
        """, (session_id,))
        return {row[0]: row[1] for row in cursor}

    def get_event_type_counts_batch(self, session_ids: List[str]) -> dict:
        """Get event counts by type for multiple sessions (batch query)."""
        if not session_ids:
            return {}

        placeholders = ",".join("?" * len(session_ids))
        cursor = self.conn.execute(f"""
            SELECT session_id, type, COUNT(*) as count
            FROM events
            WHERE session_id IN ({placeholders})
            GROUP BY session_id, type
        """, session_ids)

        result = {}
        for row in cursor:
            session_id, event_type, count = row
            if session_id not in result:
                result[session_id] = {}
            result[session_id][event_type] = count
        return result

    def get_stats(self) -> dict:
        """Get overall statistics."""
        cursor = self.conn.execute("""
            SELECT
                COUNT(DISTINCT id) as session_count,
                SUM(event_count) as event_count,
                SUM(total_tokens) as total_tokens,
                MIN(started_at) as first_session,
                MAX(started_at) as last_session
            FROM sessions
        """)
        row = cursor.fetchone()
        return dict(row) if row else {}

    def get_sync_position(self, session_id: str) -> int:
        """Get last synced position for a session."""
        cursor = self.conn.execute(
            "SELECT last_position FROM sync_state WHERE session_id = ?",
            (session_id,)
        )
        row = cursor.fetchone()
        return row[0] if row else 0

    def update_sync_position(self, session_id: str, position: int) -> None:
        """Update sync position for a session."""
        with self._write_lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO sync_state (session_id, last_position, last_sync)
                VALUES (?, ?, ?)
            """, (session_id, position, datetime.now(timezone.utc).isoformat()))
            self.conn.commit()

    def get_recent_events(
        self,
        limit: int = 50,
        event_types: Optional[List[str]] = None,
    ) -> List[dict]:
        """Get recent events with optional type filter."""
        sql = """
            SELECT id, session_id, type, ts, content
            FROM events
            WHERE content IS NOT NULL AND content != ''
        """
        params: list = []

        if event_types:
            placeholders = ",".join("?" * len(event_types))
            sql += f" AND type IN ({placeholders})"
            params.extend(event_types)

        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)

        cursor = self.conn.execute(sql, params)
        return [
            {
                "event_id": row[0],
                "session_id": row[1],
                "event_type": row[2],
                "timestamp": row[3],
                "content": row[4] or "",
                "score": 0,
                "source": "recent",
            }
            for row in cursor
        ]

    def close(self):
        """Close database connection."""
        self.conn.close()


class StorageManager:
    """Unified storage manager combining JSONL and SQLite."""

    def __init__(self, base_path: Path):
        self.base_path = base_path
        self.jsonl = JSONLStorage(base_path)
        self.sqlite = SQLiteStorage(base_path / "db" / "logging.db")

    def sync_session(self, session_id: str) -> int:
        """Sync a session from JSONL to SQLite. Returns events synced."""
        last_pos = self.sqlite.get_sync_position(session_id)
        current_pos = self.jsonl.get_last_position(session_id)

        if current_pos <= last_pos:
            return 0

        # Read new events
        events_synced = 0
        first_event_data = None
        path = self.jsonl.get_session_path(session_id)

        with open(path, "r") as f:
            f.seek(last_pos)
            for line in f:
                if line.strip():
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # Skip partial lines from concurrent writes
                    # Filter to known fields for forward-compatibility
                    event = Event(**{k: v for k, v in data.items() if k in _EVENT_FIELDS})
                    self.sqlite.insert_event(event)
                    events_synced += 1

                    # Capture first event for session metadata
                    if first_event_data is None:
                        first_event_data = data

        # Update sync position
        self.sqlite.update_sync_position(session_id, current_pos)

        # Update session record with aggregated stats
        self._update_session_from_events(session_id, first_event_data)

        return events_synced

    def _update_session_from_events(self, session_id: str, first_event_data: Optional[dict] = None) -> None:
        """Create or update session record from events table."""
        # Get aggregated stats from events
        cursor = self.sqlite.conn.execute("""
            SELECT
                MIN(ts) as started_at,
                MAX(ts) as ended_at,
                COUNT(*) as event_count
            FROM events
            WHERE session_id = ?
        """, (session_id,))
        row = cursor.fetchone()

        if not row or not row[0]:
            return

        # Extract cwd from first event data if available
        cwd = None
        if first_event_data and isinstance(first_event_data.get('data'), dict):
            cwd = first_event_data['data'].get('cwd')

        # If we don't have cwd from first_event_data, try to get from existing events
        if not cwd:
            cursor = self.sqlite.conn.execute("""
                SELECT data FROM events
                WHERE session_id = ? AND type = 'SessionStart'
                LIMIT 1
            """, (session_id,))
            data_row = cursor.fetchone()
            if data_row and data_row[0]:
                try:
                    event_data = json.loads(data_row[0])
                    cwd = event_data.get('cwd')
                except:
                    pass

        session = Session(
            id=session_id,
            started_at=row[0],
            ended_at=row[1],
            cwd=cwd,
            event_count=row[2],
        )
        self.sqlite.insert_session(session)

    def sync_all(self) -> int:
        """Sync all sessions. Returns total events synced."""
        total = 0
        for session_id in self.jsonl.list_sessions():
            total += self.sync_session(session_id)
        return total

    def search(self, query: str, limit: int = 20) -> List[dict]:
        """Search across all events."""
        return self.sqlite.search(query, limit)

    def get_search_service(self):
        """Get a fully-configured SearchService with semantic search if available."""
        from .search import SearchService
        emb_svc = None
        emb_store = None
        try:
            from .embeddings import EmbeddingService, EmbeddingStorage
            emb_db = self.base_path / "db" / "embeddings.db"
            if emb_db.exists():
                emb_svc = EmbeddingService()
                if emb_svc.is_available:
                    emb_store = EmbeddingStorage(emb_db, dimension=emb_svc.dimension)
                else:
                    emb_svc = None
        except Exception:
            pass
        self._embedding_storage = emb_store
        return SearchService(self.sqlite, embedding_service=emb_svc, embedding_storage=emb_store)

    def close(self):
        """Close all connections."""
        if hasattr(self, '_embedding_storage') and self._embedding_storage:
            self._embedding_storage.close()
        self.sqlite.close()

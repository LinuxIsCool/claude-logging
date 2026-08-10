"""
Storage Layer for Logging Plugin

Provides both JSONL (source of truth) and SQLite (indexed search) storage.
"""

import json
import logging
import sqlite3
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from dataclasses import fields as dc_fields
from datetime import datetime, timezone
from pathlib import Path

# Library code: never configure handlers/basicConfig here. This module is
# imported by hook processes whose stdout/stderr are consumed by Claude Code,
# so logging must stay silent unless the host process attaches a handler.
#
# The NullHandler is required, not decorative. With NO handler anywhere in
# the logger chain (including root), an unconfigured process falls back to
# logging.lastResort: a StreamHandler that writes WARNING+ records straight
# to stderr. hooks/log_event.py never configures logging, so without this
# NullHandler every logger.warning() call below leaks onto the hook
# process's stderr, which Claude Code consumes. Attaching a NullHandler
# here satisfies "a handler exists" and silences lastResort, while a host
# that DOES configure logging (e.g. scripts/v2/rollup_daemon.py) still
# receives the records via normal propagation.
logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

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
    ended_at: str | None = None
    cwd: str | None = None
    summary: str | None = None
    tags: list[str] = None
    event_count: int = 0
    total_tokens: int = 0

    def __post_init__(self):
        if self.tags is None:
            self.tags = []


@dataclass
class Event:
    """Event record.

    task-508 Phase 1.4 — additive fields (persona / agent_id / tool_name /
    tool_input_hash). Defaults to None so legacy JSONL events without these
    fields continue to deserialize. Schema in logging.db has matching NULL-
    default columns from migrate_001 (Phase 1.1).
    """

    id: str
    session_id: str
    type: str
    ts: str
    agent_session_num: int = 0
    data: dict = None
    content: str | None = None
    images: list | None = None  # Image references for UserPromptSubmit events

    # task-508 Phase 1.4 — additive capture-time fields
    persona: str | None = None
    agent_id: str | None = None
    tool_name: str | None = None
    tool_input_hash: str | None = None

    # Runtime-neutral provenance. Legacy records are Claude captures, so the
    # default is deliberately "claude" rather than an ambiguous NULL.
    runtime: str = "claude"
    runtime_event: str | None = None
    turn_id: str | None = None
    capture_source: str | None = None
    source_kind: str = "live"
    model: str | None = None
    permission_mode: str | None = None
    duration_ms: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None

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

        with open(path) as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)

    def list_sessions(self) -> list[str]:
        """List all session IDs."""
        return [p.stem for p in self.sessions_dir.glob("*.jsonl")]


class SQLiteStorage:
    """SQLite storage with FTS5 for search."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # busy_timeout is the ONE source of truth for write-lock patience. The
        # connect(timeout=) kwarg sets the same underlying handler, so passing
        # both means whichever runs last wins. 15s: writes are frequent and
        # short (measured ~0.02ms), so a long retry budget costs nothing and
        # absorbs contention from concurrent hook processes.
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=15000")
        # RLock, not Lock: transaction() takes the lock and calls methods that
        # take it again. Note this only serialises threads WITHIN one process;
        # cross-process safety is SQLite's WAL writer lock plus busy_timeout.
        self._write_lock = threading.RLock()
        self._init_schema()

    @contextmanager
    def transaction(self):
        """Run a group of writes as one transaction.

        Collapses sync_session's 4+N commits into 1, which is the main lever
        against SQLITE_BUSY: every hook invocation is a separate process
        contending for SQLite's single WAL writer slot. Also closes the
        partial-crash window where events landed but the sync cursor did not.
        """
        with self._write_lock:
            # sqlite3 opens an implicit transaction on execute() BEFORE the
            # statement itself runs, so a writer that raises while
            # commit=True (constraint violation, disk error) can leave
            # conn.in_transaction True with nobody to roll it back: the
            # commit that would have closed it is never reached, and the
            # except branch that would roll it back belongs to a try that
            # was never entered. If we then issued a bare BEGIN IMMEDIATE,
            # it would raise "cannot start a transaction within a
            # transaction" OUTSIDE this method's own try/except below,
            # wedging the connection permanently. Roll back any such
            # orphaned transaction first: it is leftover state from a
            # failed writer, never something we want to commit.
            if self.conn.in_transaction:
                self.conn.rollback()
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

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
                -- task-508 Phase 1.4 — additive v2-pre capture-time columns
                persona TEXT,
                agent_id TEXT,
                tool_name TEXT,
                tool_input_hash TEXT,
                runtime TEXT NOT NULL DEFAULT 'claude',
                runtime_event TEXT,
                turn_id TEXT,
                capture_source TEXT,
                source_kind TEXT NOT NULL DEFAULT 'live',
                model TEXT,
                permission_mode TEXT,
                duration_ms INTEGER,
                tokens_in INTEGER,
                tokens_out INTEGER,
                cost_usd REAL,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );

            CREATE INDEX IF NOT EXISTS idx_events_session
            ON events(session_id);

            CREATE INDEX IF NOT EXISTS idx_events_type
            ON events(type);

            CREATE INDEX IF NOT EXISTS idx_events_ts
            ON events(ts DESC);

            -- FTS5 full-text index over events (EXTERNAL CONTENT).
            -- The FTS table reads its data from `events` via content_rowid=rowid
            -- and is maintained exclusively by the triggers below.
            --
            -- NEVER INSERT/UPDATE/DELETE events_fts from application code. The
            -- previous standalone table plus `INSERT OR REPLACE` silently
            -- duplicated every re-synced row (FTS5 has no PRIMARY KEY, so
            -- OR REPLACE degrades to a plain INSERT).
            CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
                content,
                content=events,
                content_rowid=rowid,
                tokenize='porter'
            );

            -- Guards mirror the old `if event.content:` behaviour: NULL and
            -- empty-string content were never indexed and must stay unindexed.
            CREATE TRIGGER IF NOT EXISTS events_fts_ai AFTER INSERT ON events
            WHEN new.content IS NOT NULL AND new.content != ''
            BEGIN
                INSERT INTO events_fts(rowid, content) VALUES (new.rowid, new.content);
            END;

            CREATE TRIGGER IF NOT EXISTS events_fts_ad AFTER DELETE ON events
            WHEN old.content IS NOT NULL AND old.content != ''
            BEGIN
                INSERT INTO events_fts(events_fts, rowid, content)
                VALUES('delete', old.rowid, old.content);
            END;

            -- ONE trigger body, not two. SQLite fires AFTER UPDATE triggers in
            -- REVERSE creation order, so a split au_del/au_ins pair runs the
            -- INSERT first and the DELETE last: the delete wins and the row
            -- silently leaves the index on ANY update. integrity-check does NOT
            -- catch this; only a rebuild comparison does.
            -- Statements WITHIN one body run in order, so delete-then-insert is
            -- safe. Guards move to WHERE on INSERT..SELECT to stay per-statement.
            CREATE TRIGGER IF NOT EXISTS events_fts_au AFTER UPDATE ON events
            BEGIN
                INSERT INTO events_fts(events_fts, rowid, content)
                    SELECT 'delete', old.rowid, old.content
                    WHERE old.content IS NOT NULL AND old.content != '';
                INSERT INTO events_fts(rowid, content)
                    SELECT new.rowid, new.content
                    WHERE new.content IS NOT NULL AND new.content != '';
            END;

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
        # CREATE TABLE IF NOT EXISTS does not evolve existing installations.
        # Keep hook-time migration additive so a newly installed runtime edge
        # can safely write to years-old project databases on its first event.
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(events)")}
        additions = {
            "runtime": "TEXT NOT NULL DEFAULT 'claude'",
            "runtime_event": "TEXT",
            "turn_id": "TEXT",
            "capture_source": "TEXT",
            "source_kind": "TEXT NOT NULL DEFAULT 'live'",
            "model": "TEXT",
            "permission_mode": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE events ADD COLUMN {name} {declaration}")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_events_runtime ON events(runtime)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_events_turn ON events(turn_id)")
        self.conn.commit()

    def insert_session(self, session: Session, commit: bool = True) -> None:
        """Insert or update a session."""
        with self._write_lock:
            try:
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO sessions
                    (id, started_at, ended_at, cwd, summary, tags, event_count, total_tokens)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        session.id,
                        session.started_at,
                        session.ended_at,
                        session.cwd,
                        session.summary,
                        json.dumps(session.tags),
                        session.event_count,
                        session.total_tokens,
                    ),
                )
                if commit:
                    self.conn.commit()
            except Exception:
                # execute() opens an implicit transaction before the statement
                # runs, so a raise here (e.g. a constraint violation) leaves
                # conn.in_transaction True with nobody to roll it back. Only
                # do this when we own the transaction (commit=True): with
                # commit=False the caller's transaction() owns rollback, and
                # rolling back here would silently discard its whole batch.
                if commit:
                    self.conn.rollback()
                raise

    def insert_event(self, event: Event, commit: bool = True) -> None:
        """Insert or replace an event. FTS5 is maintained by triggers.

        Uses explicit DELETE + INSERT rather than INSERT OR REPLACE. REPLACE
        assigns a NEW hidden rowid, which orphans the old external-content FTS
        entry, and REPLACE does not fire AFTER DELETE triggers unless
        recursive_triggers is ON for the connection. Measured: 3 re-syncs via
        REPLACE produce 3 FTS hits; via DELETE + INSERT, 1. DELETE + INSERT is
        correct regardless of per-connection pragmas.

        Do NOT touch events_fts here. Triggers own it.

        task-508 Phase 1.4 — also writes 4 additive columns
        (persona, agent_id, tool_name, tool_input_hash) when present on the
        Event, including optional duration/token/cost measurements supplied by
        richer runtimes and archive projectors.
        """
        with self._write_lock:
            try:
                self.conn.execute("DELETE FROM events WHERE id = ?", (event.id,))
                self.conn.execute(
                    """
                    INSERT INTO events
                    (id, session_id, type, ts, agent_session_num, data, content,
                     persona, agent_id, tool_name, tool_input_hash, runtime,
                     runtime_event, turn_id, capture_source, source_kind, model, permission_mode,
                     duration_ms, tokens_in, tokens_out, cost_usd)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        event.id,
                        event.session_id,
                        event.type,
                        event.ts,
                        event.agent_session_num,
                        json.dumps(event.data),
                        event.content,
                        event.persona,
                        event.agent_id,
                        event.tool_name,
                        event.tool_input_hash,
                        event.runtime,
                        event.runtime_event,
                        event.turn_id,
                        event.capture_source,
                        event.source_kind,
                        event.model,
                        event.permission_mode,
                        event.duration_ms,
                        event.tokens_in,
                        event.tokens_out,
                        event.cost_usd,
                    ),
                )
                if commit:
                    self.conn.commit()
            except Exception:
                # See insert_session: execute() opens an implicit transaction
                # before the statement runs, so a raise here leaves
                # conn.in_transaction True with nobody to roll it back unless
                # we do it. Only when commit=True is this our transaction to
                # own; with commit=False the caller's transaction() owns
                # rollback of its whole batch.
                if commit:
                    self.conn.rollback()
                raise

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Full-text search across events."""
        cursor = self.conn.execute(
            """
            SELECT
                e.id,
                e.session_id,
                e.type,
                e.ts,
                e.content,
                bm25(events_fts) as score
            FROM events_fts
            JOIN events e ON e.rowid = events_fts.rowid
            WHERE events_fts MATCH ?
            ORDER BY score
            LIMIT ?
        """,
            (query, limit),
        )

        return [dict(row) for row in cursor]

    def get_session(self, session_id: str) -> dict | None:
        """Get session by ID."""
        cursor = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def list_sessions(
        self, limit: int = 50, offset: int = 0, date_from: str | None = None, date_to: str | None = None
    ) -> list[dict]:
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
        cursor = self.conn.execute(
            """
            SELECT type, COUNT(*) as count
            FROM events
            WHERE session_id = ?
            GROUP BY type
        """,
            (session_id,),
        )
        return {row[0]: row[1] for row in cursor}

    def get_event_type_counts_batch(self, session_ids: list[str]) -> dict:
        """Get event counts by type for multiple sessions (batch query)."""
        if not session_ids:
            return {}

        placeholders = ",".join("?" * len(session_ids))
        cursor = self.conn.execute(
            f"""
            SELECT session_id, type, COUNT(*) as count
            FROM events
            WHERE session_id IN ({placeholders})
            GROUP BY session_id, type
        """,
            session_ids,
        )

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
        cursor = self.conn.execute("SELECT last_position FROM sync_state WHERE session_id = ?", (session_id,))
        row = cursor.fetchone()
        return row[0] if row else 0

    def update_sync_position(self, session_id: str, position: int, commit: bool = True) -> None:
        """Update sync position for a session."""
        with self._write_lock:
            try:
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO sync_state (session_id, last_position, last_sync)
                    VALUES (?, ?, ?)
                """,
                    (session_id, position, datetime.now(timezone.utc).isoformat()),
                )
                if commit:
                    self.conn.commit()
            except Exception:
                # See insert_session: execute() opens an implicit transaction
                # before the statement runs, so a raise here leaves
                # conn.in_transaction True with nobody to roll it back unless
                # we do it. Only when commit=True is this our transaction to
                # own; with commit=False the caller's transaction() owns
                # rollback of its whole batch.
                if commit:
                    self.conn.rollback()
                raise

    def get_recent_events(
        self,
        limit: int = 50,
        event_types: list[str] | None = None,
    ) -> list[dict]:
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


def _recover_suffix_event(raw: bytes) -> tuple[dict, int] | None:
    """Look for a valid JSON event object at the tail of a line that failed
    to parse from offset 0.

    This handles crash recovery, not routine corruption. A flock-protected
    writer only ever emits whole newline-terminated lines WITHIN a single
    write, but that guarantee breaks across a crash: a process killed
    mid-append (OOM, SIGKILL, disk full) can leave a torn tail with no
    trailing newline. Nothing ever re-appends those orphaned bytes (each
    hook invocation is a fresh one-shot process), so the next hook's
    complete `json + "\\n"` lands immediately after them via O_APPEND, with
    no separator. readline() then returns
    `<torn prefix><valid json>\\n` as one merged line.

    The torn prefix was never completely written; it exists as a whole
    record nowhere and is genuinely unrecoverable. The JSON that follows it
    is a real, complete event and must not be discarded along with the
    prefix.

    Scans candidate object starts (byte positions of b'{"'), skipping
    position 0 since parsing from there is what already failed, and tries
    each in ascending order. Returns (data, offset) for the first candidate
    that parses to a dict with an "id" key (a real event record), else
    None.
    """
    for i in range(1, len(raw) - 1):
        if raw[i : i + 2] != b'{"':
            continue
        try:
            candidate = json.loads(raw[i:].decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(candidate, dict) and "id" in candidate:
            return candidate, i
    return None


class StorageManager:
    """Unified storage manager combining JSONL and SQLite."""

    def __init__(self, base_path: Path):
        self.base_path = base_path
        self.jsonl = JSONLStorage(base_path)
        self.sqlite = SQLiteStorage(base_path / "db" / "logging.db")
        self.last_sync_corrupt_lines = 0

    def sync_session(self, session_id: str) -> int:
        """Sync a session from JSONL to SQLite. Returns events synced.

        The cursor advances only to the end of the last line we can either
        parse or definitively give up on. Two failure modes at the tail of
        the read, handled differently:

          - No trailing newline: the writer is mid-flight on this line. We
            stop and leave the cursor before it, so the next sync retries
            this exact byte range once the writer finishes it.
          - Newline-terminated but json.loads/decode fails from offset 0:
            the flock-protected-whole-line guarantee above holds only
            WITHIN one write; it breaks across a crash. A process killed
            mid-append (OOM, SIGKILL, disk full) can leave a torn tail with
            no trailing newline. Nothing ever completes that write (each
            hook invocation is a fresh one-shot process), so the NEXT
            hook's complete `json + "\n"` lands immediately after the
            orphaned bytes via O_APPEND, with no separator, and readline()
            returns the two merged as one line. Before declaring the whole
            line dead, we scan its tail for a recoverable JSON event (see
            _recover_suffix_event): the torn prefix is genuinely lost
            (never fully written, unrecoverable), but the complete event
            that follows it is real and must not be deleted with it. If no
            suffix recovers, the line truly is corrupt (not in-flight, can
            never become valid) and we log it loudly and skip past it. See
            self.last_sync_corrupt_lines and the logger.warning calls
            below; a future watchdog should surface these instead of them
            sitting quietly in the log.

        Opened in binary mode on purpose: sync_state.last_position is a BYTE
        offset (compared against stat().st_size), and text-mode tell/seek do not
        return byte offsets for non-ASCII content.
        """
        self.last_sync_corrupt_lines = 0
        last_pos = self.sqlite.get_sync_position(session_id)
        path = self.jsonl.get_session_path(session_id)
        if not path.exists():
            return 0
        if path.stat().st_size <= last_pos:
            return 0

        events_synced = 0
        first_event_data = None
        good_through = last_pos

        # The read loop and the two trailing writes run as one transaction:
        # collapses the old 4+N commits (one per insert_event, plus
        # update_sync_position, plus insert_session) into 1, and closes the
        # partial-crash window where events land but the sync cursor does
        # not. commit=False on every writer below defers the actual COMMIT
        # to SQLiteStorage.transaction()'s __exit__.
        with self.sqlite.transaction():
            with open(path, "rb") as f:
                f.seek(last_pos)
                while True:
                    line_start = f.tell()
                    raw = f.readline()
                    if not raw:
                        break  # EOF
                    if not raw.endswith(b"\n"):
                        break  # torn tail: writer mid-flight. Retry this range next sync.
                    if raw.strip():
                        try:
                            data = json.loads(raw.decode("utf-8"))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            recovered = _recover_suffix_event(raw)
                            if recovered is not None:
                                data, suffix_offset = recovered
                                self.last_sync_corrupt_lines += 1
                                logger.warning(
                                    "sync_session: recovered event %s from a merged line in session %s "
                                    "at byte offset %d, after a %d-byte orphaned prefix (a torn, "
                                    "never-completed write from a killed process; that prefix is "
                                    "legitimately unrecoverable, the recovered event is not): %r",
                                    data.get("id"),
                                    session_id,
                                    line_start,
                                    suffix_offset,
                                    raw[:suffix_offset][:200],
                                )
                            else:
                                self.last_sync_corrupt_lines += 1
                                logger.warning(
                                    "sync_session: skipping permanently corrupt line in session %s "
                                    "at byte offset %d (never valid, not in-flight; skipping so later "
                                    "events remain reachable): %r",
                                    session_id,
                                    line_start,
                                    raw[:200],
                                )
                                data = None
                        if data is not None:
                            event = Event(**{k: v for k, v in data.items() if k in _EVENT_FIELDS})
                            self.sqlite.insert_event(event, commit=False)
                            events_synced += 1
                            if first_event_data is None:
                                first_event_data = data
                    good_through = f.tell()

            self.sqlite.update_sync_position(session_id, good_through, commit=False)
            self._update_session_from_events(session_id, first_event_data, commit=False)

        return events_synced

    def _update_session_from_events(
        self, session_id: str, first_event_data: dict | None = None, commit: bool = True
    ) -> None:
        """Create or update session record from events table."""
        # Get aggregated stats from events
        cursor = self.sqlite.conn.execute(
            """
            SELECT
                MIN(ts) as started_at,
                MAX(ts) as ended_at,
                COUNT(*) as event_count
            FROM events
            WHERE session_id = ?
        """,
            (session_id,),
        )
        row = cursor.fetchone()

        if not row or not row[0]:
            return

        # Extract cwd from first event data if available
        cwd = None
        if first_event_data and isinstance(first_event_data.get("data"), dict):
            cwd = first_event_data["data"].get("cwd")

        # If we don't have cwd from first_event_data, try to get from existing events
        if not cwd:
            cursor = self.sqlite.conn.execute(
                """
                SELECT data FROM events
                WHERE session_id = ? AND type = 'SessionStart'
                LIMIT 1
            """,
                (session_id,),
            )
            data_row = cursor.fetchone()
            if data_row and data_row[0]:
                try:
                    event_data = json.loads(data_row[0])
                    cwd = event_data.get("cwd")
                except (json.JSONDecodeError, KeyError):
                    pass

        session = Session(
            id=session_id,
            started_at=row[0],
            ended_at=row[1],
            cwd=cwd,
            event_count=row[2],
        )
        self.sqlite.insert_session(session, commit=commit)

    def sync_all(self) -> int:
        """Sync all sessions. Returns total events synced."""
        total = 0
        for session_id in self.jsonl.list_sessions():
            total += self.sync_session(session_id)
        return total

    def search(self, query: str, limit: int = 20) -> list[dict]:
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
        if hasattr(self, "_embedding_storage") and self._embedding_storage:
            self._embedding_storage.close()
        self.sqlite.close()

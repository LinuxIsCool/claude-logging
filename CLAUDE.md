# claude-logging

Per-project hook event logging — SQLite + FTS5 + JSONL session archives.

## Key Facts
- Stores per-project, NOT in a single DB. Path encoding replaces `/` with `-`.
- `/home/shawn` -> `~/.claude/local/logging/-home-shawn/`
- `conversations.db` at the root is a **dead artifact** (0 bytes, never populated). Ignore it.
- Hooks: all 25 event types captured.

## Data Schema

### SQLite: `{path-encoded}/db/logging.db`

Primary DB at `~/.claude/local/logging/-home-shawn/db/logging.db`:

```sql
CREATE TABLE sessions (
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

CREATE TABLE events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    type TEXT NOT NULL,
    ts TIMESTAMP NOT NULL,
    agent_session_num INTEGER DEFAULT 0,
    data JSON NOT NULL,
    content TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- FTS5 full-text index on events
CREATE VIRTUAL TABLE events_fts USING fts5(
    event_id, session_id, type, content,
    tokenize='porter'
);

CREATE TABLE sync_state (
    session_id TEXT PRIMARY KEY,
    last_position INTEGER DEFAULT 0,
    last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE daily_indices (
    date DATE PRIMARY KEY,
    session_count INTEGER DEFAULT 0,
    event_count INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    summary TEXT,
    tags JSON DEFAULT '[]'
);

CREATE TABLE session_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    entity_name TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    mention_count INTEGER DEFAULT 1,
    first_seen TIMESTAMP NOT NULL,
    context TEXT,
    UNIQUE(session_id, entity_name)
);

CREATE TABLE session_summaries (
    session_id TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    source TEXT DEFAULT 'compact',
    entities_extracted INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
```

### SQLite: `{path-encoded}/db/embeddings.db`

```sql
CREATE TABLE embeddings (
    event_id TEXT PRIMARY KEY,
    embedding BLOB NOT NULL
);

CREATE TABLE embedding_metadata (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    content TEXT,
    timestamp TEXT
);
```

### File Layout

```
~/.claude/local/logging/
  conversations.db             # DEAD — 0 bytes, ignore
  -home-shawn/                 # Path-encoded project root
    db/
      logging.db               # SQLite (sessions, events, events_fts, etc.)
      embeddings.db            # SQLite (event embeddings)
    sessions/
      {session-id}.jsonl       # Per-session event stream
      {session-id}.md          # Session summary (optional)
```

### JSONL: `sessions/{session-id}.jsonl`

```json
{
  "id": "evt_8115df0bd1d5",
  "type": "SessionStart",
  "ts": "2026-03-20T01:00:01.157145+00:00",
  "session_id": "00022cb1-...",
  "agent_session_num": 0,
  "data": {
    "session_id": "...",
    "transcript_path": "...",
    "cwd": "/home/shawn",
    "hook_event_name": "SessionStart",
    "source": "startup"
  },
  "content": "Session started (startup) - Model: unknown"
}
```

### Canonical Counts

```sql
SELECT type, COUNT(*) FROM events GROUP BY type;          -- event breakdown
SELECT COUNT(*) FROM sessions;                            -- session count
SELECT COUNT(*) FROM events WHERE type='UserPromptSubmit'; -- user prompts
```

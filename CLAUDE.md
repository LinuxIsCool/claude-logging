# claude-logging


**Agents**: read `AGENTS.md` first. It declares the public surface contract and conventions for working with claude-logging.
Per-project hook event logging — SQLite + FTS5 + JSONL session archives.

## Key Facts
- Stores per-project, NOT in a single DB. Path encoding replaces `/` with `-`.
- `/home/<user>` -> `~/.claude/local/logging/<project-slug>/`
- `conversations.db` at the root is a **dead artifact** (0 bytes, never populated). Ignore it.
- Hooks: all 25 event types captured.

## Data Schema

### SQLite: `{path-encoded}/db/logging.db`

Primary DB at `~/.claude/local/logging/<project-slug>/db/logging.db`:

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

-- FTS5 full-text index on events (EXTERNAL CONTENT).
-- Single `content` column; the FTS table reads from `events` via
-- content=events, content_rowid=rowid. Triggers (events_fts_ai/ad/au) own
-- the index exclusively. Application code must NEVER INSERT/UPDATE/DELETE
-- events_fts directly.
CREATE VIRTUAL TABLE events_fts USING fts5(
    content,
    content=events,
    content_rowid=rowid,
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

-- Token accounting (lib/token_meter.py). Hooks never receive token counts, so
-- these are reconstructed from the transcript named by `transcript_path`.
CREATE TABLE turns (
    request_id TEXT PRIMARY KEY,   -- makes re-scanning idempotent
    session_id TEXT NOT NULL,
    prompt_id TEXT,                -- positional attribution, see below
    ts TIMESTAMP NOT NULL,
    model TEXT,
    is_sidechain INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    cache_write INTEGER DEFAULT 0,
    cache_read INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    weighted INTEGER DEFAULT 0,    -- input-token-equivalents
    service_tier TEXT,
    stop_reason TEXT
);

CREATE TABLE prompts (
    prompt_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    ts TIMESTAMP NOT NULL,
    seq INTEGER,                   -- index within session
    text TEXT,
    chars INTEGER, words INTEGER,
    dictated INTEGER DEFAULT 0,    -- speech-to-text heuristic
    gap_seconds INTEGER,           -- think time since previous prompt
    cwd TEXT, git_branch TEXT, prompt_source TEXT,
    permission_mode TEXT, effort TEXT, model TEXT
);

CREATE TABLE meter_state (         -- resume offsets, keyed per FILE not session
    scan_key TEXT PRIMARY KEY,     -- absolute transcript path
    session_id TEXT,
    offset INTEGER DEFAULT 0,
    last_prompt TEXT,
    updated_at TIMESTAMP
);
```

### Token accounting — three things that will bite you

1. **Assistant turns carry no `promptId`.** Only user-prompt lines do. Attribution
   is positional: a forward scan carries the most recent prompt id.
2. **Subagent spend is in separate files.** `<slug>/<session-id>/subagents/agent-*.jsonl`,
   which the hook payload never names. Scanning only the main transcript misses
   roughly half the token spend. Those files have no `isSidechain` flag and no
   `promptId`; both are supplied by the caller from the file's location and by
   timestamp lookup against the parent session's prompts.
3. **`sessions.total_tokens` was dark until 2026-08-06.** The column shipped in
   v1 and no code ever wrote it. `_refresh_session_tokens()` now populates it.

```bash
# rebuild everything from transcripts (idempotent)
python3 scripts/prompt_log.py backfill
# rolling-window totals across all projects
python3 scripts/prompt_log.py usage
# regenerate the reverse-chronological prompt feed
python3 scripts/prompt_log.py render
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
  <project-slug>/             # Path-encoded project root
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

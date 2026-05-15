-- task-508 Phase 1 — cross-project rolled-up index DB schema.
-- Created at: ~/.claude/local/logging/_index/index.db
--
-- Populated by scripts/v2/rollup_index.py every 60s (cron via claude-rhythms).
-- Source of truth: per-project DBs at ~/.claude/local/logging/{slug}/db/logging.db.
-- This DB is REBUILDABLE; per-project DBs are NOT.
--
-- AGENTS.md doctrine: stays additive forever.

CREATE TABLE IF NOT EXISTS events_index (
    event_id TEXT PRIMARY KEY,
    project_slug TEXT NOT NULL,
    session_id TEXT NOT NULL,
    type TEXT NOT NULL,
    ts TIMESTAMP NOT NULL,
    persona TEXT,
    content_preview TEXT,
    has_full_content INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_index_ts ON events_index(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_index_project ON events_index(project_slug);
CREATE INDEX IF NOT EXISTS idx_events_index_persona ON events_index(persona);
CREATE INDEX IF NOT EXISTS idx_events_index_session ON events_index(session_id);
CREATE INDEX IF NOT EXISTS idx_events_index_type ON events_index(type);

-- Cross-project FTS5 — mirrors content_preview for "search every prompt I ever submitted"
CREATE VIRTUAL TABLE IF NOT EXISTS events_index_fts USING fts5(
    event_id, project_slug, session_id, type, persona, content_preview,
    tokenize='porter'
);

-- Rollup tracking — last-synced-at per project for resume-on-fail
CREATE TABLE IF NOT EXISTS rollup_state (
    project_slug TEXT PRIMARY KEY,
    last_event_ts TIMESTAMP,
    last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    event_count INTEGER DEFAULT 0,
    schema_version INTEGER DEFAULT 1
);

-- Hostname tracking — operator decision Q5 (yes via hostname suffix; multi-machine merge)
-- project_slug for non-legion machines uses '<slug>@<hostname>' convention.
-- This table records seen hostnames + last-sync for fleet visibility.
CREATE TABLE IF NOT EXISTS hostnames (
    hostname TEXT PRIMARY KEY,
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_event_ts TIMESTAMP,
    project_count INTEGER DEFAULT 0
);

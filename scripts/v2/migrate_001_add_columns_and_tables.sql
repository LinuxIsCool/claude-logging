-- task-508 Phase 1 migration — additive only.
-- Idempotent: safe to re-run. Each ALTER guarded by application-level
-- duplicate-column tolerance (see spike_a_migrate_snapshot.py).
-- v2.0.0-pre
--
-- Adds:
--   1. 8 columns to events table (persona, agent_id, tool_name, tool_input_hash,
--      duration_ms, tokens_in, tokens_out, cost_usd) — all NULL-default.
--   2. 4 new tables (prompts, annotations, pastes, tool_calls) + indexes +
--      prompts_fts FTS5 mirror.
--
-- AGENTS.md doctrine: schema grows additively. No renames. No drops.

-- 1. Add 8 columns to events
ALTER TABLE events ADD COLUMN persona TEXT;
ALTER TABLE events ADD COLUMN agent_id TEXT;
ALTER TABLE events ADD COLUMN tool_name TEXT;
ALTER TABLE events ADD COLUMN tool_input_hash TEXT;
ALTER TABLE events ADD COLUMN duration_ms INTEGER;
ALTER TABLE events ADD COLUMN tokens_in INTEGER;
ALTER TABLE events ADD COLUMN tokens_out INTEGER;
ALTER TABLE events ADD COLUMN cost_usd REAL;

-- 2. New table: prompts (denormalized UserPromptSubmit + paired AssistantResponse)
CREATE TABLE IF NOT EXISTS prompts (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    ts TIMESTAMP NOT NULL,
    persona TEXT,
    project_slug TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    prompt_tokens INTEGER,
    response_text TEXT,
    response_tokens INTEGER,
    response_event_id TEXT,
    has_images INTEGER DEFAULT 0,
    has_pastes INTEGER DEFAULT 0,
    annotation_count INTEGER DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    FOREIGN KEY (event_id) REFERENCES events(id)
);
CREATE INDEX IF NOT EXISTS idx_prompts_ts ON prompts(ts DESC);
CREATE INDEX IF NOT EXISTS idx_prompts_persona ON prompts(persona);
CREATE INDEX IF NOT EXISTS idx_prompts_session ON prompts(session_id);

-- FTS5 mirror for prompt search
CREATE VIRTUAL TABLE IF NOT EXISTS prompts_fts USING fts5(
    event_id, session_id, persona, prompt_text, response_text,
    tokenize='porter'
);

-- 3. New table: annotations (operator overlay — important / shame / great / review-later)
CREATE TABLE IF NOT EXISTS annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    note TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by TEXT,
    FOREIGN KEY (event_id) REFERENCES events(id),
    UNIQUE(event_id, kind, created_by)
);
CREATE INDEX IF NOT EXISTS idx_annotations_event ON annotations(event_id);
CREATE INDEX IF NOT EXISTS idx_annotations_kind ON annotations(kind);

-- 4. New table: pastes (long-paste content >500 chars extracted from prompts)
CREATE TABLE IF NOT EXISTS pastes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    paste_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    char_count INTEGER,
    line_count INTEGER,
    detected_language TEXT,
    FOREIGN KEY (event_id) REFERENCES events(id),
    UNIQUE(event_id, paste_index)
);
CREATE INDEX IF NOT EXISTS idx_pastes_event ON pastes(event_id);
CREATE INDEX IF NOT EXISTS idx_pastes_lang ON pastes(detected_language);

-- 5. New table: tool_calls (denormalized PreToolUse → PostToolUse pair)
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pre_event_id TEXT NOT NULL,
    post_event_id TEXT,
    session_id TEXT NOT NULL,
    ts TIMESTAMP NOT NULL,
    tool_name TEXT NOT NULL,
    input_summary TEXT,
    output_summary TEXT,
    duration_ms INTEGER,
    success INTEGER DEFAULT 1,
    error_text TEXT,
    FOREIGN KEY (pre_event_id) REFERENCES events(id),
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    UNIQUE(pre_event_id)
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_ts ON tool_calls(ts DESC);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id);

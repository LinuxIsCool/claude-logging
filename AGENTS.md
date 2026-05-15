# claude-logging — Agent Public Surface

This file declares what is and is not a stable, agent-facing public
surface of the claude-logging plugin. Adapted from MrLesk/Backlog.md
`AGENTS.md` "Agent POV" doctrine. Track C of task-439 fleet adoption.

If you are an AI agent operating in this repository or installing this
plugin, read this file first.

claude-logging is the **conversation history archive** for Legion.
It hooks all 25 Claude Code event types, persists JSONL + SQLite+FTS5
per-project, exposes search/browse skills, and serves a web UI for
human review.

---

## Public surface

Agents MAY rely on the stability of:

1. **Slash commands**:
   - `/logging:browse` — browse recent Claude Code sessions.
   - `/logging:search <query>` — search conversation history.
   - `/logging:stats` — session counts + event breakdowns.

2. **Skills**:
   - `log-search` (`skills/log-search/SKILL.md`) — semantic + FTS5
     search across all logged sessions.
   - `obsidian` (`skills/obsidian/SKILL.md`) — emit logging data
     into Obsidian-compatible markdown for vault integration.
   - `stats` (`skills/stats/SKILL.md`) — aggregate stats / time
     series across sessions.
   - `web-ui` (`skills/web-ui/SKILL.md`) — launch the local web
     viewer (vanilla stdlib http.server).

3. **The data contract**:
   - Per-project storage:
     `~/.claude/local/logging/{encoded-project-path}/db/logging.db`
     (SQLite + FTS5) + `sessions/*.jsonl` (raw event log).
   - Path encoding: `/home/shawn` → `-home-shawn`; replaces `/` with
     `-`. Use this exact encoding when locating a project's DB.
   - SQLite schema (table `events`): `id TEXT PRIMARY KEY, type TEXT,
     session_id TEXT, ts TIMESTAMP, data JSON, content TEXT,
     agent_session_num INTEGER`.
   - **v2-pre additive columns (task-508 Phase 1, applied 2026-05-15)**:
     `persona TEXT, agent_id TEXT, tool_name TEXT, tool_input_hash TEXT,
     duration_ms INTEGER, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL`.
     Schema migration tooling at `scripts/v2/run_migration_001.py`. All
     205 per-project DBs migrated; tool_name + tool_input_hash + duration_ms
     backfilled. persona/agent_id/tokens/cost populated at capture-time
     (Phase 1.4 wave).
   - **v2-pre new tables**: `prompts` (denormalized UserPromptSubmit pairs),
     `annotations` (operator overlay), `pastes` (long-paste artifacts),
     `tool_calls` (denormalized pre/post tool pair). FTS5 mirror at
     `prompts_fts`.
   - **v2-pre cross-project index**: `~/.claude/local/logging/_index/index.db`
     with `events_index`, `events_index_fts`, `rollup_state`, `hostnames`
     tables. Populated by 60s rollup cron (Phase 1.5).
   - FTS5 virtual table `events_fts` mirrors `(content)` column.
   - Event types: all 25 Claude Code hook events
     (UserPromptSubmit, PreToolUse, PostToolUse, Stop, SubagentStop,
     SessionStart, SessionEnd, Notification, PreCompact, PostCompact,
     etc.).

4. **Hooks** (registered in `plugin.json`):
   - All 25 hook event types route to `hooks/log_event.py -e <event>`
     which writes JSONL + SQLite atomically.
   - SessionEnd additionally runs `hooks/session_summary.py` for
     consolidation.

5. **Documented agent instructions**:
   - This file (`AGENTS.md`)
   - Plugin `CLAUDE.md`
   - Skill descriptions

## NOT a public surface

Agents MUST NOT reference, depend on, or import:

1. **The dead `~/.claude/local/logging/conversations.db`** file —
   it's a 0-byte artifact that was never populated. Use per-project
   DBs instead.

2. **Embedding table internals** (`embeddings.db`) — semantic search
   is exposed via skills, not the raw embeddings DB. Schema may
   change.

3. **The `log_event.py` hook contract** — it accepts `-e <event>` and
   reads stdin per Claude Code's hook protocol. Don't call it directly
   from non-Claude-Code contexts.

4. **JSONL file format internals** — read via the skills or the SQLite
   FTS5 index. Per-event JSON keys may grow additively.

5. **`session_summary.py` internal output** — it's consumed by the
   logging stats skill, not external agents.

## Conventions for agents working with claude-logging

### Searching history

- **Prefer FTS5 (via skills)** over raw scan. SQLite FTS5 with token
  matching is sub-second across 31K+ events.
- **Session ID is stable** within a project for the session lifetime
  but NOT across projects (different DBs).
- **Time range queries** use `ts` (unix epoch seconds). All events
  are timestamped on hook fire.
- **`UserPromptSubmit` events contain raw user prompts** — useful
  when reconstructing intent.

### Writing data

- **Do NOT write to the logging DB directly** — let hooks do it. If
  you need to inject a synthetic event, file a feature request; do
  not bypass the schema invariants.
- **Hooks are idempotent on `(session_id, ts, type, content_hash)`**
  by design — duplicate fires are no-ops.

### Web UI

- **`/logging:web-ui` launches a local ThreadingHTTPServer** on a
  free port (typically 6421). Vanilla stdlib + Alpine.js, no Node
  toolchain.
- **Browser auto-opens** unless `--no-open` flag is passed.

### When in doubt

1. Read `~/.claude/plugins/local/legion-plugins/plugins/claude-logging/CLAUDE.md`.
2. Read `skills/log-search/SKILL.md`.
3. Read this file.
4. Run `/logging:stats` to confirm DB health.
5. Ask the user — do not invent new conventions.

## Boundary doctrine for cross-plugin agents

If an agent from another Legion plugin interacts with claude-logging:

- **Read via SQLite directly** if you need raw event access. The DB
  path is stable; schema (`events` table) is stable.
- **Or via skills** if you want structured search results.
- **Never write to the DB** — hooks are the only writer. Concurrent
  writes from non-hook sources can corrupt FTS5 indices.
- **For long-form recall** (>30 days of history), use the `log-search`
  skill — it handles pagination and ranking.

## When the public surface changes

If a documented surface needs to change:

1. The change is announced in this file with a version bump.
2. SQLite `events` table grows additively — new columns added with
   NULL defaults, never renamed.
3. New event types added in Claude Code (the upstream tool) are
   absorbed automatically — hooks route them by name.
4. Path encoding changes (if Claude Code ever changes the encoding
   convention) require an outbox draft + migration plan.

---

## Provenance

- Doctrine: MrLesk/Backlog.md `AGENTS.md` "Agent POV" → Legion
  Phase 3 of task-435.
- Plugin vision: `~/.claude/plugins/local/legion-plugins/plugins/claude-logging/CLAUDE.md`.
- Template: `~/.claude/plugins/local/legion-plugins/plugins/_templates/AGENTS_TEMPLATE.md`.
- Adoption tracking: task-437 + task-439 Track C.
- This file last updated: 2026-05-12.

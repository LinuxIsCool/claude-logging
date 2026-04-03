# Claude Code Logging Plugin

[![CI](https://github.com/LinuxIsCool/claude-logging/actions/workflows/ci.yml/badge.svg)](https://github.com/LinuxIsCool/claude-logging/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

Comprehensive conversation logging with hybrid search, full subagent transcript capture, and visualization for Claude Code.

> [!NOTE]
> This is a Claude Code plugin that hooks into every lifecycle event, stores complete conversation history, and makes it all searchable with hybrid keyword + semantic search.

## What It Does

- **Captures everything** — all 25 hook event types including full subagent transcripts
- **Makes it searchable** — FTS5 keyword search + semantic embeddings with RRF fusion
- **Renders it readable** — auto-generated markdown session logs with collapsible sections
- **Keeps it local** — all data stays on your machine, no external services required

## How It Works

```
Claude Code
  │ JSON via STDIN (hooks)
  ▼
log_event.py
  │
  ├──▶ JSONL Storage (source of truth)
  │    sessions/*.jsonl
  │
  ├──▶ SQLite + FTS5 (indexed search)
  │    db/logging.db
  │
  ├──▶ Embeddings (semantic search)
  │    db/embeddings.db
  │
  └──▶ Markdown (human-readable)
       sessions/*.md
```

On `SubagentStop`, the plugin reads the subagent's full transcript JSONL and enriches the event with all assistant text, tool calls, token usage, and metadata. This means subagent work is fully searchable — not just "Agent 'Explore' finished" but the complete content of what the agent did.

## Installation

### Option A: Plugin Marketplace

```
/plugin marketplace add LinuxIsCool/claude-logging
```

### Option B: Manual Install

1. Clone the plugin:
   ```bash
   git clone https://github.com/LinuxIsCool/claude-logging.git ~/.claude/plugins/claude-logging
   ```

2. Install dependencies:
   ```bash
   cd ~/.claude/plugins/claude-logging
   uv sync

   # For semantic search (optional):
   uv sync --extra embeddings
   ```

3. Enable the plugin in Claude Code:
   ```
   /plugin enable .
   ```

## Storage

All logs are stored at `~/.claude/local/logging/<encoded-project-path>/`:

```
~/.claude/local/logging/<encoded-project>/
├── sessions/          # JSONL files (one per session)
├── db/
│   ├── logging.db     # SQLite with FTS5 full-text search
│   └── embeddings.db  # Semantic search vectors (optional)
├── images/            # Extracted user images
└── markdown/          # Auto-generated session logs
```

The project path is encoded by replacing `/` with `-`, mirroring Claude Code's own `~/.claude/projects/` convention. For example, `/home/user/my-app` → `-home-user-my-app`.

## Search

### Skill

Use the built-in log-search skill:
```
/log-search authentication implementation
/log-search --type=prompt --date=week
```

### Archivist Agent

Invoke the Archivist for conversational recall:
```
What did we discuss about the database schema?
How have we handled auth before?
```

### Python API

```python
from pathlib import Path
from lib.storage import StorageManager

sm = StorageManager(Path.home() / '.claude/local/logging' / str(Path.home()).replace('/', '-'))
svc = sm.get_search_service()
results, ms = svc.hybrid_search('your query', limit=20, use_semantic=True)
for r in results:
    print(f'[{r.timestamp[:10]}] [{r.event_type}] {r.content[:120]}')
```

### REST API

```bash
# Start the API server
cd ~/.claude/plugins/claude-logging && uv run api/server.py

# Search
curl -X POST http://localhost:3001/api/search \
  -H "Content-Type: application/json" \
  -d '{"query": "your query", "limit": 20, "use_semantic": true}'
```

API endpoints:
- `GET /api/stats` — overall statistics
- `POST /api/search` — hybrid search
- `GET /api/sessions` — list sessions
- `GET /api/sessions/{id}` — session details
- `GET /api/events/stream` — SSE live event stream

## Event Types

Claude Code exposes 25 hook event types (as of v2.1.84). This plugin captures all of them.

**Session lifecycle:**

| Type | Description |
|------|-------------|
| `SessionStart` | Session begins or resumes |
| `SessionEnd` | Session terminates |

**User interaction:**

| Type | Description |
|------|-------------|
| `UserPromptSubmit` | User submits a prompt |
| `Notification` | System notification sent |
| `Elicitation` | MCP server requests user input |
| `ElicitationResult` | User responds to MCP elicitation |

**Tool lifecycle:**

| Type | Description |
|------|-------------|
| `PreToolUse` | Before tool call executes (can block) |
| `PermissionRequest` | Permission dialog shown |
| `PostToolUse` | After tool call succeeds |
| `PostToolUseFailure` | After tool call fails |

**Agent lifecycle:**

| Type | Description |
|------|-------------|
| `SubagentStart` | Subagent spawned |
| `SubagentStop` | Subagent finished (with full transcript) |
| `TeammateIdle` | Agent team teammate about to go idle |
| `TaskCreated` | Task created via TaskCreate tool |
| `TaskCompleted` | Task marked as completed |

**Turn lifecycle:**

| Type | Description |
|------|-------------|
| `Stop` | Claude finishes responding |
| `StopFailure` | Turn ends due to API error |

**Environment:**

| Type | Description |
|------|-------------|
| `InstructionsLoaded` | CLAUDE.md or rules loaded into context |
| `ConfigChange` | Configuration file changes during session |
| `CwdChanged` | Working directory changes |
| `FileChanged` | Watched file changes on disk |

**Worktree:**

| Type | Description |
|------|-------------|
| `WorktreeCreate` | Worktree created via `--worktree` |
| `WorktreeRemove` | Worktree removed at session/subagent exit |

**Compaction:**

| Type | Description |
|------|-------------|
| `PreCompact` | Before context compaction |
| `PostCompact` | After context compaction completes |

## Subagent Transcript Capture

When a subagent finishes, the plugin reads its full transcript and enriches the `SubagentStop` event:

- **Content**: All assistant text responses → FTS5 and embedding indexing
- **Metadata**: Model, tool names, turn count, aggregated token usage, timestamps
- **Rendering**: Full untruncated content in collapsible markdown blocks

This means every subagent's work is searchable by keyword and semantically, not just by boundary events.

## Entity Extraction

The plugin includes lightweight NER (Named Entity Recognition) that extracts:
- **People** — from your contacts database, or capitalized two-word names as fallback
- **Ventures** — dynamically loaded from `~/.claude/local/ventures/`
- **Projects** — dynamically loaded from installed plugins
- **Dates**, **Money**, **Durations** — regex-based

Patterns are loaded at import time from your local data, so they adapt to any user's environment automatically.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/sync_backfill.py` | Backfill JSONL sessions into SQLite |
| `scripts/embed_backfill.py` | Generate embeddings for semantic search |
| `scripts/extract_session_text.py` | Extract searchable text from transcripts |

All scripts accept `--project-path` to work with any project directory.

### Optional Integrations

Additional scripts in `contrib/` demonstrate integration with external services (FalkorDB, PostgreSQL). See `contrib/README.md`.

## Web Interface (Experimental)

> The web interface is functional but not fully tested. Contributions welcome.

```bash
cd ~/.claude/plugins/claude-logging
./scripts/start-web.sh
```

Access at http://127.0.0.1:3002. Features:
- **Sessions**: Browse with search, event type filters, collapsible transcripts
- **Statistics**: Overview metrics and activity summary

## Configuration

All configuration is optional. The plugin works with zero configuration.

| Environment Variable | Default | Purpose |
|---------------------|---------|---------|
| `CLAUDE_PROJECT_DIR` | Current working directory | Project path for storage isolation |
| `CLAUDE_PLUGIN_ROOT` | Auto-detected | Plugin installation directory |
| `LOGGING_API_PORT` | `3001` | REST API server port |

## FAQ

**Where is my data stored?**
`~/.claude/local/logging/<encoded-project-path>/`. The project path encoding replaces `/` with `-`, matching Claude Code's own convention.

**How do I enable semantic search?**
Install the optional embeddings dependency: `uv sync --extra embeddings`. Then run the backfill: `uv run scripts/embed_backfill.py`. See `examples/embedding-setup.md` for details.

**What happens when Claude Code adds new hook types?**
The plugin handles unknown event types gracefully — they're stored with generic content extraction. To add specific handling, see CONTRIBUTING.md.

## Performance

- FTS5 search: <1ms for 10K+ events
- Hybrid search with RRF: <5ms
- JSONL append: <1ms (file locking for concurrency)
- SQLite sync: ~1000 events/sec
- Subagent transcript extraction: <75ms even for 13MB transcripts

## Platform Support

- **Linux/macOS**: Full support with file locking (`fcntl`)
- **Windows**: Functional but without file locking (concurrent sessions may have rare write conflicts)

## License

MIT — see [LICENSE](LICENSE)

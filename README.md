# Claude Code Logging Plugin

Comprehensive conversation logging with hybrid search, embeddings, and visualization for Claude Code.

## Features

- **Complete Event Capture**: Logs all 9 hook event types (SessionStart, SessionEnd, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SubagentStop, PreCompact, Notification)
- **Dual Storage**: JSONL files (source of truth) + SQLite (indexed search)
- **Hybrid Search**: FTS5 keyword search with optional semantic search using RRF fusion
- **Local Embeddings**: sentence-transformers for semantic search (optional)
- **REST API**: FastAPI server for programmatic access
- **Real-time Updates**: Server-Sent Events for live event streaming
- **Obsidian Integration**: View logs as an Obsidian vault
- **Web Interface**: React dashboard with search, sessions, and embedding visualization

## Installation

1. Clone the plugin to your Claude Code plugins directory:
   ```bash
   git clone https://github.com/LinuxIsCool/claude-logging.git ~/.claude/plugins/claude-logging
   ```

2. Install dependencies:
   ```bash
   cd ~/.claude/plugins/claude-logging
   uv pip install -e .

   # For embeddings support:
   uv pip install -e ".[embeddings]"
   ```

## Usage

### Automatic Logging

Once installed, all Claude Code interactions are automatically logged to:
```
$CLAUDE_PROJECT_DIR/.claude/local/logging/
├── sessions/          # JSONL files (one per session)
├── db/               # SQLite database with FTS5
├── indices/          # Daily/weekly/monthly indices
└── embeddings/       # Vector embeddings (optional)
```

### Search

Use the log-search skill:
```
/log-search authentication implementation
/log-search --type=prompt --date=week
```

Or invoke the Archivist agent:
```
What did we discuss about the database schema?
```

### Statistics

```
/log-stats
/log-stats --period=week
```

### API Server

Start the API server:
```bash
cd ~/.claude/plugins/logging
uv run api/server.py
```

Then access:
- `GET /api/stats` - Overall statistics
- `POST /api/search` - Search logs
- `GET /api/sessions` - List sessions
- `GET /api/sessions/{id}` - Get session details
- `GET /api/events/stream` - SSE event stream

### Obsidian

Open logs in Obsidian:
```
/obsidian
```

### Web Interface

Launch the full web dashboard:
```bash
cd ~/.claude/plugins/logging
./scripts/start-web.sh
```

Or start servers individually:
```bash
# API Server (port 3001)
uv run python -m uvicorn api.server:app --host 127.0.0.1 --port 3001

# Web Server (port 3002)
cd web && npm run dev
```

Access the dashboard at http://127.0.0.1:3002

Features:
- **Sessions Tab**: Browse sessions with integrated search, event type filters, and collapsible transcript view with markdown rendering
- **Embeddings Tab**: Interactive 2D projection of conversation embeddings
- **Statistics Tab**: Overview metrics and activity summary

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Claude Code                          │
│  ┌─────────────────────────────────────────────────┐   │
│  │                   Hooks                          │   │
│  │  SessionStart, UserPromptSubmit, PreToolUse,    │   │
│  │  PostToolUse, Stop, SubagentStop, PreCompact    │   │
│  └───────────────────────┬─────────────────────────┘   │
└──────────────────────────┼──────────────────────────────┘
                           │ JSON via STDIN
                           ▼
┌─────────────────────────────────────────────────────────┐
│                  log_event.py                           │
│  • Parse event data                                     │
│  • Extract searchable content                           │
│  • Calculate agent_session_num                          │
│  • Append to JSONL with file locking                    │
└───────────────────────────┬─────────────────────────────┘
                           │
            ┌──────────────┴──────────────┐
            ▼                             ▼
┌───────────────────────┐     ┌───────────────────────────┐
│   JSONL Storage       │     │   SQLite Storage          │
│   (Source of Truth)   │────▶│   (Indexed Search)        │
│   sessions/*.jsonl    │sync │   db/logging.db           │
└───────────────────────┘     │   • FTS5 full-text        │
                              │   • BM25 ranking          │
                              └─────────────┬─────────────┘
                                            │
                              ┌─────────────┴─────────────┐
                              ▼                           ▼
                    ┌─────────────────┐        ┌──────────────────┐
                    │ Search Service  │        │  API Server      │
                    │ • Keyword (FTS5)│        │  • REST API      │
                    │ • Semantic      │        │  • SSE streaming │
                    │ • RRF Fusion    │        │  • Web interface │
                    └─────────────────┘        └──────────────────┘
```

## Configuration

Settings in `plugin.json`:

| Setting | Default | Description |
|---------|---------|-------------|
| `storage_path` | `.claude/local/logging` | Data directory |
| `enable_embeddings` | `false` | Generate embeddings |
| `enable_summaries` | `false` | AI-generated summaries |
| `api_port` | `3001` | API server port |

## Event Schema

Each event in JSONL format:
```json
{
  "id": "evt_abc123def456",
  "type": "UserPromptSubmit",
  "ts": "2024-01-15T10:30:00.000Z",
  "session_id": "session_xyz789",
  "agent_session_num": 0,
  "data": { ... },
  "content": "Searchable content extracted from data"
}
```

## Performance

- FTS5 search: <1ms for 10k events
- Hybrid search with RRF: <5ms
- JSONL append: <1ms (file locking for concurrency)
- SQLite sync: ~1000 events/sec

## License

MIT

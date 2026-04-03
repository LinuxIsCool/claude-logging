# Contributing

## Development Setup

```bash
git clone https://github.com/LinuxIsCool/claude-logging.git
cd claude-logging
uv sync
uv run pytest tests/
```

## How It Works

1. Claude Code fires hook events (JSON via STDIN)
2. `hooks/log_event.py` processes each event:
   - Appends to session JSONL (source of truth)
   - Syncs to SQLite + FTS5 on turn boundaries
   - Generates markdown session logs
   - Optionally embeds for semantic search
3. `lib/` provides the storage and search layers
4. `api/` serves a REST API for the web UI

## Adding Support for New Event Types

`log_event.py` handles unknown event types generically. To add specific handling:

1. Add an emoji to `EMOJIS` dict
2. Add content extraction logic in `extract_content()`
3. Add rendering logic in `generate_markdown()` if needed
4. Register the hook in `plugin.json`

## Running Tests

```bash
uv run pytest tests/ -v          # Core tests
uv run pytest contrib/tests/ -v  # Optional integration tests (need extra deps)
```

## Platform Notes

- File locking uses `fcntl` (Linux/macOS). Windows gets a no-op fallback.
- Tested on Python 3.10+.

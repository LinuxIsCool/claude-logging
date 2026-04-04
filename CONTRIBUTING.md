# Contributing

Thank you for your interest in improving claude-logging.

## Development Setup

```bash
git clone https://github.com/LinuxIsCool/claude-logging.git
cd claude-logging
uv sync --dev
uv run pytest tests/
uv run ruff check .
```

## How It Works

Everything flows through one file. When Claude Code fires a hook event, it sends JSON on STDIN to `hooks/log_event.py`. That script:

1. Parses the event and extracts searchable content
2. Appends the event to a session JSONL file (the permanent record)
3. On turn boundaries (Stop, SubagentStop), syncs new events into SQLite with FTS5
4. On SubagentStop, reads the subagent's full transcript and enriches the event
5. On Stop, extracts any images the user pasted and captures the assistant's response
6. Generates a markdown session log for human reading

The `lib/` directory provides the storage engine (JSONL + SQLite), the search service (FTS5 + optional semantic), and embedding support. The `api/` directory serves a FastAPI REST interface that the web UI consumes.

## Adding Support for New Event Types

Claude Code adds new hook event types over time. The plugin handles unknown types gracefully — they're captured with their raw data even without specific handling. To add specific support for a new type:

1. Add an emoji to the `EMOJIS` dict in `hooks/log_event.py`
2. Add a branch in `extract_content()` to produce searchable text
3. Add rendering logic in `generate_markdown()` if needed
4. Register the hook in `plugin.json`

## Running Tests

```bash
uv run pytest tests/ -v            # All tests (173 currently)
uv run pytest contrib/tests/ -v    # Optional integration tests (extra deps required)
```

The test suite covers the core event processing path, all API endpoints, the search layer (FTS5, RRF fusion, hybrid search), embedding storage, entity extraction, and markdown rendering.

## Code Quality

This project uses [ruff](https://docs.astral.sh/ruff/) for linting and formatting:

```bash
uv run ruff check .          # Lint
uv run ruff format --check . # Format check
```

A `.pre-commit-config.yaml` is provided if you use pre-commit hooks.

## Platform Notes

- File locking uses `fcntl` (Linux/macOS). Windows gets a no-op fallback — concurrent sessions may occasionally conflict, but data won't be lost.
- Tested on Python 3.10 through 3.13, on Linux, macOS, and Windows.
- CI runs on all three platforms via GitHub Actions.

## Submitting Changes

1. Fork and create a feature branch
2. Write tests for new functionality
3. Ensure `uv run pytest tests/` and `uv run ruff check .` both pass
4. Submit a pull request describing what changed and why

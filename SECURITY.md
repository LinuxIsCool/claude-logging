# Security

## Your Data Stays on Your Machine

This plugin stores conversation history locally. It never transmits data to external services, never phones home, and includes no telemetry or analytics of any kind.

### What Gets Stored

| Data | Location | Contains |
|------|----------|----------|
| Session events | `sessions/*.jsonl` | Event type, timestamp, tool names, prompts, responses |
| Search index | `db/logging.db` | Same data, indexed for full-text search |
| Embeddings | `db/embeddings.db` | Semantic vectors (optional, only if you enable it) |
| Images | `images/` | Screenshots and images pasted into prompts |

All data lives under `~/.claude/local/logging/<encoded-project-path>/`.

### What Is NOT Stored

- No API keys, tokens, or credentials
- No data is sent to any remote server
- No analytics, tracking, or telemetry

### Clearing Data

Remove all logged data for one project:
```bash
rm -rf ~/.claude/local/logging/<encoded-project-path>/
```

Remove all logged data across all projects:
```bash
rm -rf ~/.claude/local/logging/
```

### Reporting Vulnerabilities

If you discover a security issue, please email the maintainer directly rather than opening a public issue. See the repository for contact information.

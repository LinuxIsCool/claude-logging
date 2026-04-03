# Security

## Data Storage

All data is stored **locally on your machine**. This plugin never transmits data to external services.

### What Gets Stored

| Data | Location | Contains |
|------|----------|----------|
| Session events | `~/.claude/local/logging/<project>/sessions/*.jsonl` | Event type, timestamp, tool names, user prompts, assistant responses |
| Search index | `~/.claude/local/logging/<project>/db/logging.db` | Same as above, indexed for FTS5 search |
| Embeddings | `~/.claude/local/logging/<project>/db/embeddings.db` | Semantic vectors (optional) |
| Images | `~/.claude/local/logging/<project>/images/` | Images pasted into prompts |

### What Is NOT Stored

- No API keys, tokens, or credentials
- No data is sent to any remote server
- No telemetry or analytics

## Clearing Data

To remove all logged data for a project:

```bash
rm -rf ~/.claude/local/logging/<encoded-project-path>/
```

To remove all logged data across all projects:

```bash
rm -rf ~/.claude/local/logging/
```

## Reporting Vulnerabilities

If you discover a security issue, please email the maintainer directly rather than opening a public issue.

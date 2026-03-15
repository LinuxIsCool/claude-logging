---
description: Search conversation history for past discussions and context
---

# Log Search Command

Search through your Claude Code conversation history.

## Usage

```
/log-search <query>
/log-search <query> --type=prompt
/log-search <query> --date=week
/log-search <query> --semantic
```

## Arguments

- `query`: The search term or phrase
- `--type`: Filter by event type (prompt, response, tool, session)
- `--date`: Filter by date (today, week, month, YYYY-MM-DD)
- `--semantic`: Enable semantic search (finds conceptually related results)

## Examples

```
/log-search authentication
/log-search "database schema" --type=prompt
/log-search error --date=today
/log-search "how do we handle caching" --semantic
```

## Implementation

Use the Skill tool to invoke the log-search skill, which will:
1. Search via hybrid FTS5 + semantic search (SQLite + embeddings)
2. Fall back to JSONL grep if database unavailable
3. Present results with session context
4. Offer follow-up actions

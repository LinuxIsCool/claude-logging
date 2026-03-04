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
```

## Arguments

- `query`: The search term or phrase
- `--type`: Filter by event type (prompt, response, tool, session)
- `--date`: Filter by date (today, week, month, YYYY-MM-DD)

## Examples

```
/log-search authentication
/log-search "database schema" --type=prompt
/log-search error --date=today
```

## Implementation

Use the Skill tool to invoke the log-search skill, which will:
1. Search JSONL files in `.claude/local/logging/sessions/`
2. Present results with session context
3. Offer follow-up actions

---
name: stats
description: Display logging statistics including session counts, event breakdowns, and usage patterns
allowed-tools: Bash, Read
---

# Statistics Skill

You are helping the user view their Claude Code usage statistics.

## Data Sources

Statistics are derived from:
1. **JSONL files**: `.claude/local/logging/sessions/*.jsonl`
2. **SQLite database**: `.claude/local/logging/db/logging.db`

## How to Gather Statistics

### Count Sessions

```bash
# Total sessions
ls .claude/local/logging/sessions/*.jsonl 2>/dev/null | wc -l

# Sessions today
find .claude/local/logging/sessions -name "*.jsonl" -mtime 0 | wc -l
```

### Count Events

```bash
# Total events across all sessions
wc -l .claude/local/logging/sessions/*.jsonl 2>/dev/null | tail -1

# Events by type
grep -h '"type":' .claude/local/logging/sessions/*.jsonl | sort | uniq -c | sort -rn
```

### Using SQLite (if synced)

```sql
-- Session statistics
SELECT
    COUNT(*) as total_sessions,
    SUM(event_count) as total_events,
    SUM(total_tokens) as total_tokens,
    MIN(started_at) as first_session,
    MAX(started_at) as last_session
FROM sessions;

-- Events by type
SELECT type, COUNT(*) as count
FROM events
GROUP BY type
ORDER BY count DESC;

-- Daily activity
SELECT DATE(started_at) as date, COUNT(*) as sessions
FROM sessions
GROUP BY DATE(started_at)
ORDER BY date DESC
LIMIT 7;
```

## Response Format

Present statistics clearly:

```
📊 Logging Statistics

Sessions: 42 total (5 today, 18 this week)
Events: 1,234 total
  - UserPromptSubmit: 312
  - AssistantResponse: 298
  - PreToolUse: 412
  - PostToolUse: 212

Activity:
  - Most active: Tuesday (12 sessions)
  - Average session: 29 events
  - First session: 2024-01-15
```

## Follow-up Options

After showing stats, offer:
- "Browse recent sessions" - List sessions
- "Search for term" - Find specific content
- "View session [ID]" - Open a specific session

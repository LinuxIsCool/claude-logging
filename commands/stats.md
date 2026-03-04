---
description: Show logging statistics including session counts and event breakdowns
---

# Log Statistics Command

Display statistics about your Claude Code usage.

## Usage

```
/log-stats
/log-stats --period=week
/log-stats --period=today
```

## Output

Shows:
- Total sessions
- Total events
- Events by type breakdown
- Most active days
- Common topics (if embeddings enabled)

## Implementation

Query the SQLite database at `.claude/local/logging/db/logging.db`:

```sql
SELECT
    COUNT(DISTINCT id) as sessions,
    SUM(event_count) as events,
    SUM(total_tokens) as tokens,
    MIN(started_at) as first,
    MAX(started_at) as last
FROM sessions;
```

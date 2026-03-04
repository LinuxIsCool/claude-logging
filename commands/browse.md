---
description: Browse recent Claude Code sessions
---

# Log Browse Command

Browse recent Claude Code sessions.

## Usage

```
/log-browse
/log-browse --limit=5
/log-browse --date=today
```

## Output

Shows a list of recent sessions with:
- Session ID
- Start time
- Duration
- Event count
- Summary (if available)

## Implementation

List and display sessions from `~/.claude/local/logging/<project>/sessions/`:

```bash
# List recent sessions (project path encoded with / -> -)
PROJECT_ENCODED=$(echo "$CLAUDE_PROJECT_DIR" | tr '/' '-')
ls -lt ~/.claude/local/logging/$PROJECT_ENCODED/sessions/*.jsonl | head -10

# Get session summary
head -1 ~/.claude/local/logging/$PROJECT_ENCODED/sessions/{id}.jsonl | jq -r '.data.cwd // "Unknown"'
```

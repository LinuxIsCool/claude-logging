---
name: log-search
description: Search conversation history for past discussions, decisions, and context. Use when you need to recall what was discussed about a topic, find previous solutions, retrieve historical context from past sessions, answer "What did we discuss about X?", get log statistics, or browse specific sessions.
allowed-tools: Bash, Read
---

# Log Search Skill

You are helping the user search their Claude Code conversation history.

## Capabilities

1. **Keyword Search**: Find exact matches in prompts, responses, and tool outputs
2. **Semantic Search**: Find conceptually related content (when embeddings enabled)
3. **Time Filtering**: Narrow results by date range
4. **Type Filtering**: Focus on specific event types

## How to Search

The logging plugin stores all Claude Code interactions centrally in `~/.claude/local/logging/<project>/` (under the home directory).

### Preferred: Python SearchService (hybrid search)

```bash
cd ~/.claude/plugins/local/legion-plugins/plugins/claude-logging && uv run python -c "
from pathlib import Path
from lib.storage import StorageManager
sm = StorageManager(Path.home() / '.claude/local/logging/-home-shawn')
svc = sm.get_search_service()
results, ms = svc.hybrid_search('USER_QUERY', limit=20, use_semantic=True)
import json
for r in results:
    print(json.dumps({'ts': r.timestamp, 'type': r.event_type, 'content': r.content, 'session': r.session_id, 'source': r.source}))
"
```

This uses FTS5 keyword search + semantic embeddings with Reciprocal Rank Fusion (RRF). Semantic search finds conceptually similar content even when keywords don't match.

### Using the Search API

```bash
# Search for a term (with optional semantic)
curl -X POST http://localhost:3001/api/search \
  -H "Content-Type: application/json" \
  -d '{"query": "USER_QUERY", "limit": 20, "use_semantic": true}'
```

### Fallback: Direct JSONL Search

If neither the Python SearchService nor the API is available, search JSONL files directly:

```bash
# Find sessions containing a term (project path encoded with / -> -)
PROJECT_ENCODED=$(echo "$CLAUDE_PROJECT_DIR" | tr '/' '-')
grep -l "search_term" ~/.claude/local/logging/$PROJECT_ENCODED/sessions/*.jsonl

# Search within a specific session
grep "search_term" ~/.claude/local/logging/$PROJECT_ENCODED/sessions/{session_id}.jsonl
```

## Response Format

Present results as a numbered list:

1. **[Date] Session: Brief Title**
   - Event type: UserPromptSubmit / AssistantResponse / ToolUse
   - Preview: First 100 characters of matching content...
   - Session ID: `abc123` (for follow-up queries)

2. **[Date] Session: Another Title**
   ...

## Follow-up Actions

After showing results, offer these options:
- "Show more results" - Expand the search
- "Open session [ID]" - View full session transcript
- "Search within session [ID]" - Narrow the search
- "Refine search with filters" - Add date/type filters

## Event Types

| Type | Description |
|------|-------------|
| `UserPromptSubmit` | User's messages/questions |
| `AssistantResponse` | Claude's responses |
| `PreToolUse` | Tool execution start |
| `PostToolUse` | Tool execution result |
| `SessionStart` | Session began |
| `Stop` | Session ended |
| `SubagentStop` | Subagent completed |

## Example Session

User: "What did we discuss about authentication?"

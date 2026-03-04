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

The logging plugin stores all Claude Code interactions in `.claude/local/logging/`.

### Using the Search API

```bash
# Search for a term
curl -X POST http://localhost:3001/api/search \
  -H "Content-Type: application/json" \
  -d '{"query": "USER_QUERY", "limit": 20}'
```

### Direct JSONL Search

If the API is not running, search JSONL files directly:

```bash
# Find sessions containing a term
grep -l "search_term" .claude/local/logging/sessions/*.jsonl

# Search within a specific session
grep "search_term" .claude/local/logging/sessions/{session_id}.jsonl
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

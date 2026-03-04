---
name: obsidian
description: Open the logging directory in Obsidian as a vault for visual exploration of conversation history
allowed-tools: Bash
---

# Obsidian Integration Skill

You are helping the user open their logging data in Obsidian for visual exploration.

## Prerequisites

- Obsidian must be installed
- The logging plugin must be active

## Opening in Obsidian

The logging directory can be opened as an Obsidian vault:

```bash
# macOS
open -a Obsidian "${CLAUDE_PROJECT_DIR}/.claude/local/logging"

# Linux
obsidian "obsidian://open?path=${CLAUDE_PROJECT_DIR}/.claude/local/logging"

# Alternative: open folder directly
xdg-open "${CLAUDE_PROJECT_DIR}/.claude/local/logging"
```

## Vault Structure

When opened as a vault, users will see:
- `sessions/` - Individual session transcripts
- `indices/` - Daily, weekly, monthly summaries
- `db/` - SQLite database (ignore in Obsidian)

## Recommended Obsidian Setup

For best experience, suggest:

1. **Install plugins**:
   - Dataview: Query session metadata
   - Graph View: Visualize connections
   - Calendar: Navigate by date

2. **Create a home note** at `logging/README.md`:
   ```markdown
   # Claude Code Logs

   ## Recent Sessions
   ```dataview
   TABLE started_at, event_count
   FROM "sessions"
   SORT started_at DESC
   LIMIT 10
   ```
   ```

3. **Configure graph view**:
   - Color by folder
   - Group sessions by date

## Response Format

After opening:
```
📓 Opened logging vault in Obsidian

Location: .claude/local/logging
Sessions: 42 available
Indices: daily, weekly, monthly

Tip: Use Dataview queries to explore sessions by date or topic.
```

# Centralized Storage + Open-Source Readiness

**Date**: 2026-03-03
**Status**: Approved

## Summary

Change the default log storage from project-local (`$PROJECT/.claude/local/logging/`) to centralized (`~/.claude/local/logging/<encoded-project-path>/`). Remove all configuration — centralized is the only mode. Also address open-source readiness issues and add example log data.

## Storage Design

### Path Encoding

Mirror Claude Code's own convention from `~/.claude/projects/`:
- Replace `/` with `-`
- Leading `/` becomes leading `-`

**Examples**:
| Project Path | Log Storage Path |
|-------------|-----------------|
| `/home/user/Workspace/app` | `~/.claude/local/logging/-home-user-Workspace-app/` |
| `/home/user` | `~/.claude/local/logging/-home-user/` |
| `/tmp/test` | `~/.claude/local/logging/-tmp-test/` |

### Resolution

The `get_storage_path()` function becomes:

```python
def get_storage_path(cwd: Optional[str] = None) -> Path:
    project_dir = cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    encoded = project_dir.replace("/", "-")
    return Path.home() / ".claude" / "local" / "logging" / encoded
```

No env vars, no config files, no overrides.

### Why No Config

- Claude Code doesn't expose its settings infrastructure to plugins
- Fewer moving parts = fewer bugs in a logging system that must never fail
- Users who truly need custom paths can fork

## Open-Source Readiness

| Fix | File(s) |
|-----|---------|
| Add MIT LICENSE | `LICENSE` (new) |
| Add `watchfiles` optional dep | `pyproject.toml` |
| Update hardcoded path refs | `commands/*.md`, `skills/*/SKILL.md`, `agents/archivist.md`, `README.md` |

## Example Log

New file `examples/sample-session.jsonl` with synthetic 6-event session demonstrating the JSONL schema: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, AssistantResponse.

## Files Changed

1. `hooks/log_event.py` — `get_storage_path()`
2. `lib/__init__.py` — `get_storage_path()`
3. `api/server.py` — `STORAGE_PATH` constant
4. `tools/repair_sessions.py` — candidate path detection
5. `commands/search.md`, `commands/browse.md`, `commands/stats.md` — path references
6. `skills/log-search/SKILL.md` — path references
7. `agents/archivist.md` — path references
8. `README.md` — installation, architecture, config sections
9. `pyproject.toml` — watchfiles optional dep
10. New: `LICENSE`
11. New: `examples/sample-session.jsonl`

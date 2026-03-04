# Centralized Storage + Open-Source Readiness — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Change default log storage to centralized `~/.claude/local/logging/<encoded-project-path>/`, add LICENSE, fix docs, add example log.

**Architecture:** Single `encode_project_path()` function replaces `/` with `-` (mirroring `~/.claude/projects/`). All `get_storage_path()` callsites updated. No config — centralized only.

**Tech Stack:** Python 3.10+, JSONL, SQLite/FTS5, FastAPI, Next.js

---

### Task 1: Core — Update `get_storage_path()` in `hooks/log_event.py`

**Files:**
- Modify: `hooks/log_event.py:41-54`

**Step 1: Replace `get_storage_path()` function**

Replace lines 41-54 of `hooks/log_event.py` with:

```python
def encode_project_path(project_dir: str) -> str:
    """Encode a project directory path for use as a directory name.

    Mirrors Claude Code's ~/.claude/projects/ convention:
    /home/shawn/Workspace/app -> -home-shawn-Workspace-app
    """
    return project_dir.replace("/", "-")


def get_storage_path(cwd: Optional[str] = None) -> Path:
    """Get the centralized storage path for logging data.

    Logs are stored at ~/.claude/local/logging/<encoded-project-path>/
    where the project path is encoded by replacing / with -

    Args:
        cwd: Working directory from hook data (preferred source)
    """
    project_dir = cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    encoded = encode_project_path(project_dir)
    return Path.home() / ".claude" / "local" / "logging" / encoded
```

**Step 2: Verify hook still runs**

```bash
cd ~/claude-logging
echo '{"session_id":"test","cwd":"/home/shawn/test-project","data":{}}' | uv run hooks/log_event.py -e SessionStart
ls ~/.claude/local/logging/-home-shawn-test-project/sessions/
```

Expected: `test.jsonl` file created at the centralized path.

**Step 3: Clean up test data**

```bash
rm -rf ~/.claude/local/logging/-home-shawn-test-project/
```

**Step 4: Commit**

```bash
git add hooks/log_event.py
git commit -m "feat: centralize log storage at ~/.claude/local/logging/<encoded-path>/"
```

---

### Task 2: Core — Update `get_storage_path()` in `lib/__init__.py`

**Files:**
- Modify: `lib/__init__.py:16-23`

**Step 1: Replace `get_storage_path()` function**

Replace lines 16-23 of `lib/__init__.py` with:

```python
def encode_project_path(project_dir: str) -> str:
    """Encode a project directory path for use as a directory name.

    Mirrors Claude Code's ~/.claude/projects/ convention:
    /home/shawn/Workspace/app -> -home-shawn-Workspace-app
    """
    return project_dir.replace("/", "-")


def get_storage_path() -> Path:
    """Get the centralized storage path for logging data.

    Logs are stored at ~/.claude/local/logging/<encoded-project-path>/
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    encoded = encode_project_path(project_dir)
    return Path.home() / ".claude" / "local" / "logging" / encoded
```

**Step 2: Commit**

```bash
git add lib/__init__.py
git commit -m "feat: centralize storage path in lib/__init__.py"
```

---

### Task 3: Core — Update `STORAGE_PATH` in `api/server.py`

**Files:**
- Modify: `api/server.py:28-31`

**Step 1: Replace STORAGE_PATH constant**

Replace lines 28-31 of `api/server.py` with:

```python
# Configuration
_project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
_encoded = _project_dir.replace("/", "-")
STORAGE_PATH = Path.home() / ".claude" / "local" / "logging" / _encoded
```

**Step 2: Commit**

```bash
git add api/server.py
git commit -m "feat: centralize storage path in API server"
```

---

### Task 4: Core — Update `repair_sessions.py` candidate paths

**Files:**
- Modify: `tools/repair_sessions.py:160-165`

**Step 1: Replace candidate path detection**

Replace lines 160-165 of `tools/repair_sessions.py` with:

```python
        # Centralized storage path
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", str(Path.cwd()))
        encoded = project_dir.replace("/", "-")
        candidates = [
            Path.home() / ".claude" / "local" / "logging" / encoded,
        ]
        storage_path = next((p for p in candidates if p.exists()), None)
```

**Step 2: Commit**

```bash
git add tools/repair_sessions.py
git commit -m "feat: update repair tool for centralized storage"
```

---

### Task 5: Docs — Update path references in commands

**Files:**
- Modify: `commands/search.md:37`
- Modify: `commands/browse.md:28-35`
- Modify: `commands/stats.md:28`

**Step 1: Update `commands/search.md`**

Replace line 37:
```
1. Search JSONL files in `.claude/local/logging/sessions/`
```
With:
```
1. Search JSONL files in `~/.claude/local/logging/<project>/sessions/`
```

**Step 2: Update `commands/browse.md`**

Replace lines 28-35:
```
List and display sessions from `.claude/local/logging/sessions/`:

```bash
# List recent sessions
ls -lt .claude/local/logging/sessions/*.jsonl | head -10

# Get session summary
head -1 .claude/local/logging/sessions/{id}.jsonl | jq -r '.data.cwd // "Unknown"'
```
```
With:
```
List and display sessions from `~/.claude/local/logging/<project>/sessions/`:

```bash
# List recent sessions (project path encoded with / -> -)
PROJECT_ENCODED=$(echo "$CLAUDE_PROJECT_DIR" | tr '/' '-')
ls -lt ~/.claude/local/logging/$PROJECT_ENCODED/sessions/*.jsonl | head -10

# Get session summary
head -1 ~/.claude/local/logging/$PROJECT_ENCODED/sessions/{id}.jsonl | jq -r '.data.cwd // "Unknown"'
```
```

**Step 3: Update `commands/stats.md`**

Replace line 28:
```
Query the SQLite database at `.claude/local/logging/db/logging.db`:
```
With:
```
Query the SQLite database at `~/.claude/local/logging/<project>/db/logging.db`:
```

**Step 4: Commit**

```bash
git add commands/
git commit -m "docs: update command path references for centralized storage"
```

---

### Task 6: Docs — Update path references in skills and agent

**Files:**
- Modify: `skills/log-search/SKILL.md:20,37-40`
- Modify: `agents/archivist.md:31,53-57`

**Step 1: Update `skills/log-search/SKILL.md`**

Replace line 20:
```
The logging plugin stores all Claude Code interactions in `.claude/local/logging/`.
```
With:
```
The logging plugin stores all Claude Code interactions in `~/.claude/local/logging/<project>/` (centralized under home directory).
```

Replace lines 37-40 (the direct JSONL search section):
```bash
# Find sessions containing a term
grep -l "search_term" .claude/local/logging/sessions/*.jsonl

# Search within a specific session
grep "search_term" .claude/local/logging/sessions/{session_id}.jsonl
```
With:
```bash
# Find sessions containing a term (project path encoded with / -> -)
PROJECT_ENCODED=$(echo "$CLAUDE_PROJECT_DIR" | tr '/' '-')
grep -l "search_term" ~/.claude/local/logging/$PROJECT_ENCODED/sessions/*.jsonl

# Search within a specific session
grep "search_term" ~/.claude/local/logging/$PROJECT_ENCODED/sessions/{session_id}.jsonl
```

**Step 2: Update `agents/archivist.md`**

Replace line 31:
```
grep -l "search_term" .claude/local/logging/sessions/*.jsonl
```
With:
```
PROJECT_ENCODED=$(echo "$CLAUDE_PROJECT_DIR" | tr '/' '-')
grep -l "search_term" ~/.claude/local/logging/$PROJECT_ENCODED/sessions/*.jsonl
```

Replace line 33:
```
cat .claude/local/logging/sessions/{session_id}.jsonl | jq -r '.content // empty'
```
With:
```
cat ~/.claude/local/logging/$PROJECT_ENCODED/sessions/{session_id}.jsonl | jq -r '.content // empty'
```

Replace lines 53-57:
```
- Sessions: `.claude/local/logging/sessions/*.jsonl`
- Database: `.claude/local/logging/db/logging.db`
- Indices: `.claude/local/logging/indices/`
```
With:
```
Logs are centralized at `~/.claude/local/logging/<encoded-project-path>/`:
- Sessions: `~/.claude/local/logging/<project>/sessions/*.jsonl`
- Database: `~/.claude/local/logging/<project>/db/logging.db`
- Images: `~/.claude/local/logging/<project>/images/`

The project path is encoded by replacing `/` with `-` (e.g. `/home/user/my-project` -> `-home-user-my-project`).
```

**Step 3: Commit**

```bash
git add skills/ agents/
git commit -m "docs: update skill and agent path references for centralized storage"
```

---

### Task 7: Docs — Update README.md

**Files:**
- Modify: `README.md`

**Step 1: Update storage path section (lines 37-43)**

Replace:
```
$CLAUDE_PROJECT_DIR/.claude/local/logging/
├── sessions/          # JSONL files (one per session)
├── db/               # SQLite database with FTS5
├── indices/          # Daily/weekly/monthly indices
└── embeddings/       # Vector embeddings (optional)
```
With:
```
~/.claude/local/logging/<encoded-project-path>/
├── sessions/          # JSONL files (one per session)
├── db/               # SQLite database with FTS5
├── images/           # Extracted user images
└── embeddings/       # Vector embeddings (optional)
```

**Step 2: Update API/web server paths (lines 68-69, 97-101)**

Replace all occurrences of `cd ~/.claude/plugins/logging` with `cd ~/.claude/plugins/claude-logging`.

**Step 3: Remove Configuration section (lines 152-162)**

Replace the Configuration section:
```
## Configuration

Settings in `plugin.json`:

| Setting | Default | Description |
|---------|---------|-------------|
| `storage_path` | `.claude/local/logging` | Data directory |
| `enable_embeddings` | `false` | Generate embeddings |
| `enable_summaries` | `false` | AI-generated summaries |
| `api_port` | `3001` | API server port |
```
With:
```
## Storage

Logs are stored centrally at `~/.claude/local/logging/<encoded-project-path>/`.

The project path is encoded by replacing `/` with `-`, mirroring Claude Code's own `~/.claude/projects/` convention. For example, a project at `/home/user/my-app` stores logs at `~/.claude/local/logging/-home-user-my-app/`.

This means all logs from all projects are in one place, making cross-project search straightforward.
```

**Step 4: Update architecture diagram paths**

In the architecture diagram (lines 113-149), update the storage box text from `$CLAUDE_PROJECT_DIR/.claude/local/logging` to `~/.claude/local/logging/<project>`.

**Step 5: Commit**

```bash
git add README.md
git commit -m "docs: update README for centralized storage and standalone install"
```

---

### Task 8: Add LICENSE file

**Files:**
- Create: `LICENSE`

**Step 1: Create MIT license**

```
MIT License

Copyright (c) 2026 LinuxIsCool

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

**Step 2: Commit**

```bash
git add LICENSE
git commit -m "chore: add MIT license"
```

---

### Task 9: Add `watchfiles` optional dependency

**Files:**
- Modify: `pyproject.toml:20-25`

**Step 1: Add watchfiles to optional deps**

After the `embeddings` section in `[project.optional-dependencies]`, add:

```toml
streaming = [
    "watchfiles>=0.21.0",
]
```

And update the `all` section to include it:

```toml
all = [
    "claude-logging-plugin[embeddings,streaming,dev]",
]
```

**Step 2: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add watchfiles as optional streaming dependency"
```

---

### Task 10: Add example session log

**Files:**
- Create: `examples/sample-session.jsonl`

**Step 1: Create example directory and file**

```bash
mkdir -p ~/claude-logging/examples
```

Write `examples/sample-session.jsonl` with a synthetic 6-event session. Each line is a single JSON object:

```jsonl
{"id":"evt_a1b2c3d4e5f6","type":"SessionStart","ts":"2026-03-03T10:00:00.000Z","session_id":"session_example_001","agent_session_num":0,"data":{"source":"startup","model":"claude-sonnet-4-6","cwd":"/home/user/my-project"},"content":"Session started (startup) - Model: claude-sonnet-4-6"}
{"id":"evt_b2c3d4e5f6a7","type":"UserPromptSubmit","ts":"2026-03-03T10:00:05.000Z","session_id":"session_example_001","agent_session_num":0,"data":{"prompt":"Show me the main entry point of this project"},"content":"Show me the main entry point of this project"}
{"id":"evt_c3d4e5f6a7b8","type":"PreToolUse","ts":"2026-03-03T10:00:06.000Z","session_id":"session_example_001","agent_session_num":0,"data":{"tool_name":"Glob","tool_input":{"pattern":"**/main.{py,ts,js}"}},"content":"Finding files: **/main.{py,ts,js}"}
{"id":"evt_d4e5f6a7b8c9","type":"PostToolUse","ts":"2026-03-03T10:00:06.500Z","session_id":"session_example_001","agent_session_num":0,"data":{"tool_name":"Glob","tool_response":{"numFiles":1,"filePaths":["src/main.py"]}},"content":"Found 1 files"}
{"id":"evt_e5f6a7b8c9d0","type":"Stop","ts":"2026-03-03T10:00:08.000Z","session_id":"session_example_001","agent_session_num":0,"data":{"transcript_path":"/home/user/.claude/projects/-home-user-my-project/session_example_001.jsonl"},"content":"Claude finished responding"}
{"id":"evt_f6a7b8c9d0e1","type":"AssistantResponse","ts":"2026-03-03T10:00:08.000Z","session_id":"session_example_001","agent_session_num":0,"data":{"response":"The main entry point is `src/main.py`. It initializes the application and starts the server."},"content":"The main entry point is `src/main.py`. It initializes the application and starts the server."}
```

**Step 2: Commit**

```bash
git add examples/
git commit -m "docs: add example session log for schema reference"
```

---

### Task 11: Final verification

**Step 1: Run a quick smoke test of the hook**

```bash
cd ~/claude-logging
echo '{"session_id":"verify","cwd":"/tmp/verify-test","data":{"source":"startup","model":"test"}}' | uv run hooks/log_event.py -e SessionStart
cat ~/.claude/local/logging/-tmp-verify-test/sessions/verify.jsonl
```

Expected: Valid JSONL event at the centralized path.

**Step 2: Clean up test data**

```bash
rm -rf ~/.claude/local/logging/-tmp-verify-test/
```

**Step 3: Check all Python imports still resolve**

```bash
cd ~/claude-logging
uv run python -c "from lib import get_storage_path; print(get_storage_path())"
```

Expected: Prints `~/.claude/local/logging/-home-shawn-claude-logging` (or similar based on CWD).

**Step 4: Final commit with design doc**

```bash
git add docs/
git commit -m "docs: add design doc and implementation plan"
```

---

### Summary

| Task | What | Commit |
|------|------|--------|
| 1 | `hooks/log_event.py` — centralize `get_storage_path()` | `feat: centralize log storage` |
| 2 | `lib/__init__.py` — centralize `get_storage_path()` | `feat: centralize storage path in lib` |
| 3 | `api/server.py` — centralize `STORAGE_PATH` | `feat: centralize storage path in API` |
| 4 | `tools/repair_sessions.py` — update candidates | `feat: update repair tool` |
| 5 | `commands/*.md` — update path refs | `docs: update commands` |
| 6 | `skills/`, `agents/` — update path refs | `docs: update skills and agent` |
| 7 | `README.md` — full update | `docs: update README` |
| 8 | `LICENSE` — MIT | `chore: add MIT license` |
| 9 | `pyproject.toml` — watchfiles dep | `chore: add watchfiles dep` |
| 10 | `examples/sample-session.jsonl` | `docs: add example session` |
| 11 | Final verification + design doc commit | `docs: add design doc` |

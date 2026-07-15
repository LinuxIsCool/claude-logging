# claude-logging Foundation and Realtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make live capture structurally unable to fail silently, make the SQLite index provably correct under re-sync, and drive capture lag to zero.

**Architecture:** Hooks write per-session JSONL (source of truth) and sync into SQLite (derived index). This plan locks in the manifest fix with a test that asserts the plugin's contract with its host, converts `events_fts` to external-content FTS5 so duplication becomes structurally impossible, fixes a cursor bug that silently drops events, then moves sync from turn boundaries to inline-per-event (measured at 0.02ms against a 35-83ms hook).

**Tech Stack:** Python 3.11+, SQLite (WAL + FTS5), `uv` for execution, pytest.

**Scope:** Phases 1-3 of `docs/superpowers/specs/2026-07-15-logging-reliability-design.md`. Phase 4 (transcript backfill) and Phase 5 (watchdog) get a separate plan once this lands, because the backfill's design depends on the FTS migration being real and verified.

## Global Constraints

- Run everything through `uv run` from the plugin root. The lockfile and `.venv` are present; no network needed.
- Python >= 3.10 (`ruff` targets py310, line-length 120).
- **`events` schema is additive-only** (AGENTS.md invariant). Never rename or drop a column. New columns take NULL defaults.
- **Never write to `events_fts` from application code** after Task 3. Triggers own it. This is the whole point of the migration.
- **Never assert FTS correctness with `SELECT COUNT(*) FROM events_fts`.** With external content that query delegates to the base table and always passes. It is a false green. Assert with `events_fts MATCH` or `'integrity-check'`.
- The live store is at `~/.claude/local/logging/<encoded-project>/db/logging.db` (88MB, ~17k events). **Back it up before any migration writes.**
- Hooks load at session start and cannot be hot-swapped. Verify with a fresh `claude -p` session, never by asserting the current session.
- `log_event.py` must never write to stdout/stderr or return non-zero. Failures go to `errors.log`. Breaking this injects noise into every session.

## Background: why this plan exists

Capture was silently dead for 15 days (2026-06-30 to 2026-07-15). The manifest was at `plugin.json`; Claude Code reads it only from `.claude-plugin/plugin.json`. Skills, commands, and agents load by directory convention and kept working, so every visible signal said healthy while the entire hook path was dead. Fixed and proven (store 15 -> 17 sessions). Task 1 exists so that failure mode can never ship again.

## File Structure

| File | Responsibility |
|---|---|
| `tests/test_plugin_registration.py` (create) | Asserts the plugin's contract with Claude Code: manifest location, hooks declared, and (live) that a real session captures. |
| `.github/workflows/ci.yml` (modify) | Runs the structural half of the registration test in CI. |
| `plugin.json` (delete) | Vestigial. `.claude-plugin/plugin.json` is canonical. |
| `lib/storage.py` (modify) | FTS5 external-content schema + triggers; `insert_event` DELETE+INSERT; torn-line cursor; single transaction; cwd guard. |
| `lib/search.py:72` (modify) | FTS join moves from `event_id` to `rowid`. |
| `scripts/migrate_002_events_fts_external_content.py` (create) | One-time, idempotent migration of live DBs. |
| `hooks/log_event.py` (modify) | Inline sync per event; cache the agent-session scan. |
| `tests/test_storage_sync.py` (create) | Regression tests for both data-loss bugs. |

---

### Task 1: The registration test

The test that would have caught the outage on day one. It asserts the plugin's contract with its host, which no existing test does.

**Files:**
- Create: `tests/test_plugin_registration.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: nothing.
- Produces: `PLUGIN_ROOT` (a `pathlib.Path` to the plugin root) for later tests.

- [ ] **Step 1: Write the failing test**

Create `tests/test_plugin_registration.py`:

```python
"""Asserts claude-logging's contract with Claude Code itself.

claude-logging captured nothing from 2026-06-30 to 2026-07-15 because its
manifest sat at plugin.json instead of .claude-plugin/plugin.json. Claude Code
reads the manifest ONLY from .claude-plugin/plugin.json. Skills, commands and
agents load by directory convention, so they kept working and masked the
failure completely.

The structural tests run anywhere, including CI. The live test needs the
`claude` binary and is skipped without it.
"""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"

# Every hook event the plugin intends to capture.
EXPECTED_HOOK_EVENTS = {
    "SessionStart", "SessionEnd", "UserPromptSubmit", "Notification",
    "Elicitation", "ElicitationResult", "PreToolUse", "PermissionRequest",
    "PostToolUse", "PostToolUseFailure", "SubagentStart", "SubagentStop",
    "TeammateIdle", "TaskCreated", "TaskCompleted", "Stop", "StopFailure",
    "InstructionsLoaded", "ConfigChange", "CwdChanged", "FileChanged",
    "WorktreeCreate", "WorktreeRemove", "PreCompact", "PostCompact",
}


def test_manifest_exists_at_claude_plugin_path():
    assert MANIFEST.exists(), (
        "Claude Code reads the plugin manifest ONLY from "
        ".claude-plugin/plugin.json. A manifest at the repo root is silently "
        "ignored and hooks never register. This is exactly what caused the "
        "2026-06-30 outage."
    )


def test_manifest_is_valid_json_with_required_fields():
    m = json.loads(MANIFEST.read_text())
    for field in ("name", "version", "description"):
        assert field in m, f"manifest missing required field: {field}"


def test_manifest_declares_every_expected_hook_event():
    m = json.loads(MANIFEST.read_text())
    hooks = m.get("hooks")
    assert isinstance(hooks, dict), (
        "manifest.hooks must be an inline object of hook events. If it is a "
        "string pointing at ./hooks/hooks.json, that file already auto-loads "
        "and pointing at it triggers 'Duplicate hooks file detected' / "
        "hook-load-failed."
    )
    assert set(hooks) == EXPECTED_HOOK_EVENTS, (
        f"hook events drifted. missing={EXPECTED_HOOK_EVENTS - set(hooks)} "
        f"unexpected={set(hooks) - EXPECTED_HOOK_EVENTS}"
    )


def test_no_vestigial_root_manifest():
    assert not (PLUGIN_ROOT / "plugin.json").exists(), (
        "A root plugin.json is ignored by Claude Code. Keeping one alongside "
        ".claude-plugin/plugin.json invites edits to the dead file."
    )


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude binary not available")
def test_claude_plugin_validate_passes():
    r = subprocess.run(
        ["claude", "plugin", "validate", str(PLUGIN_ROOT)],
        capture_output=True, text=True, timeout=60,
    )
    assert "✘" not in r.stdout, f"claude plugin validate failed:\n{r.stdout}"
```

- [ ] **Step 2: Run the tests to verify current state**

Run: `cd /home/shawn/Workspace/legion-plugins/plugins/claude-logging && uv run pytest tests/test_plugin_registration.py -v`

Expected: `test_no_vestigial_root_manifest` FAILS (root `plugin.json` still exists). The others PASS, because the manifest fix already landed. That one failure is Task 2's job.

- [ ] **Step 3: Add the structural tests to CI**

In `.github/workflows/ci.yml`, add a step to the existing test job. Do not add `claude plugin validate` to CI; the binary is not available there, and the skipif handles it.

```yaml
      - name: Plugin registration contract
        run: uv run pytest tests/test_plugin_registration.py -v
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_plugin_registration.py .github/workflows/ci.yml
git commit -m "test: assert plugin manifest contract with Claude Code

The 15-day capture outage had no test that could have caught it. This
asserts the manifest lives where Claude Code actually reads it and that
every intended hook event is declared."
```

---

### Task 2: Single canonical manifest

**Files:**
- Delete: `plugin.json`
- Test: `tests/test_plugin_registration.py` (already written)

**Interfaces:**
- Consumes: `tests/test_plugin_registration.py` from Task 1.
- Produces: nothing.

- [ ] **Step 1: Confirm nothing reads the root manifest**

Run:
```bash
cd /home/shawn/Workspace/legion-plugins
grep -rn "plugins/claude-logging/plugin.json" --include="*.py" --include="*.sh" --include="*.json" --include="*.yml" . | grep -v ".claude-plugin" | grep -v node_modules
```
Expected: no output. If there ARE readers, update them to `.claude-plugin/plugin.json` before deleting.

- [ ] **Step 2: Verify the two manifests are equivalent before deleting**

Run:
```bash
cd /home/shawn/Workspace/legion-plugins/plugins/claude-logging
python3 -c "
import json
root = json.load(open('plugin.json'))
canon = json.load(open('.claude-plugin/plugin.json'))
missing = {k: root[k] for k in root if k not in canon}
print('keys only in root manifest:', list(missing) or 'none')
"
```
Expected: `skills`, `commands`, `agents` may appear. These are safe to drop: the debug log confirms they load by directory convention (`Loaded 4 skills / 3 commands / 1 agents from plugin logging default directory`). Do NOT drop `hooks`.

- [ ] **Step 3: Delete the root manifest**

```bash
git rm plugin.json
```

- [ ] **Step 4: Verify hooks STILL fire after deletion**

This is the step that matters. `claude -p` spawns a fresh session which loads hooks anew.

```bash
STORE=~/.claude/local/logging/-home-shawn/sessions
BEFORE=$(ls $STORE/*.jsonl | wc -l)
claude -p "reply with exactly: pong" --model claude-haiku-4-5-20251001 < /dev/null
sleep 3   # Stop/SessionEnd hooks write AFTER the response returns
AFTER=$(ls $STORE/*.jsonl | wc -l)
echo "$BEFORE -> $AFTER"
```
Expected: count increments by 1. If it does not, restore `plugin.json` immediately (`git checkout plugin.json`) and stop; something else reads it.

- [ ] **Step 5: Run the full registration test**

Run: `uv run pytest tests/test_plugin_registration.py -v`
Expected: all PASS, including `test_no_vestigial_root_manifest`.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: drop vestigial root plugin.json

Claude Code ignores it. Keeping it alongside .claude-plugin/plugin.json
invites future edits to the dead file, which is how the outage happened."
```

---

### Task 3: FTS5 external-content migration (schema + writers)

The core correctness fix. Makes duplication structurally impossible rather than closing one code path.

**Why the current code is broken:** `insert_event()` does `INSERT OR REPLACE INTO events_fts`, but FTS5 virtual tables have no PRIMARY KEY, so `OR REPLACE` degrades to a plain `INSERT`. Re-syncing any event duplicates its row. Proven: same `event_id` twice yields 2 rows.

**Why the obvious fixes are also broken** (all three measured):
- `INSERT OR REPLACE` into `events` with external-content triggers yields **3 FTS hits for 3 re-syncs**. REPLACE assigns a **new hidden rowid**, orphaning the old FTS entry, and the AFTER DELETE trigger does not fire on REPLACE unless `recursive_triggers=ON`.
- Adding `PRAGMA recursive_triggers=ON` works but depends on every connection setting it. Any future writer that forgets silently duplicates. Rejected: fragile.
- `INSERT ... ON CONFLICT(id) DO UPDATE` yields **0 hits**. The row vanishes from the index entirely.

**The fix:** explicit `DELETE` then `INSERT`, which fires the real AFTER DELETE trigger regardless of pragmas. Measured: 1 hit for 3 re-syncs, and a genuine content change correctly drops the old term and indexes the new one.

**Files:**
- Modify: `lib/storage.py:196-202` (FTS schema), `lib/storage.py:276-319` (`insert_event`), `lib/storage.py:333` (search join)
- Modify: `lib/search.py:72` (search join)
- Create: `tests/test_storage_sync.py`

**Interfaces:**
- Consumes: `Event`, `SQLiteStorage` from `lib/storage.py`.
- Produces: `SQLiteStorage.insert_event(event: Event) -> None` (unchanged signature; FTS now trigger-maintained). `events_fts` becomes external-content with a single `content` column; joins use `events_fts.rowid = events.rowid`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_storage_sync.py`:

```python
"""Regression tests for the two confirmed data-loss bugs.

IMPORTANT: never assert FTS correctness with `SELECT COUNT(*) FROM events_fts`.
With external content that delegates to the base table and always passes. It is
a false green. Assert with MATCH or 'integrity-check'.
"""

import json

import pytest

from lib.storage import Event, SQLiteStorage, StorageManager


def _match(db, term):
    return db.conn.execute(
        "SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH ?", (term,)
    ).fetchone()[0]


def test_resyncing_same_event_does_not_duplicate_fts(tmp_path):
    """The confirmed FTS5 duplicate bug. Backfill re-syncs by design."""
    db = SQLiteStorage(tmp_path / "t.db")
    e = Event(id="evt-1", session_id="s", type="UserPromptSubmit",
              ts="2026-07-15T00:00:00+00:00", content="hello world")
    for _ in range(3):
        db.insert_event(e)
    assert _match(db, "hello") == 1
    db.close()


def test_changing_event_content_reindexes(tmp_path):
    """Old term must stop matching; new term must start matching."""
    db = SQLiteStorage(tmp_path / "t.db")
    db.insert_event(Event(id="evt-1", session_id="s", type="T",
                          ts="2026-07-15T00:00:00+00:00", content="hello world"))
    db.insert_event(Event(id="evt-1", session_id="s", type="T",
                          ts="2026-07-15T00:00:00+00:00", content="goodbye moon"))
    assert _match(db, "hello") == 0
    assert _match(db, "goodbye") == 1
    db.close()


def test_empty_content_is_not_indexed(tmp_path):
    """Mirrors the old `if event.content:` guard: empty string must not index."""
    db = SQLiteStorage(tmp_path / "t.db")
    db.insert_event(Event(id="evt-1", session_id="s", type="T",
                          ts="2026-07-15T00:00:00+00:00", content=""))
    db.insert_event(Event(id="evt-2", session_id="s", type="T",
                          ts="2026-07-15T00:00:00+00:00", content=None))
    db.conn.execute("INSERT INTO events_fts(events_fts) VALUES('integrity-check')")
    db.close()


def test_fts_index_integrity_after_resync(tmp_path):
    db = SQLiteStorage(tmp_path / "t.db")
    for i in range(20):
        db.insert_event(Event(id=f"evt-{i}", session_id="s", type="T",
                              ts="2026-07-15T00:00:00+00:00", content=f"payload {i}"))
    for i in range(20):
        db.insert_event(Event(id=f"evt-{i}", session_id="s", type="T",
                              ts="2026-07-15T00:00:00+00:00", content=f"payload {i}"))
    db.conn.execute("INSERT INTO events_fts(events_fts) VALUES('integrity-check')")
    assert _match(db, "payload") == 20
    db.close()


def test_search_still_returns_results(tmp_path):
    """The join moved from event_id to rowid; search must still work."""
    db = SQLiteStorage(tmp_path / "t.db")
    db.insert_event(Event(id="evt-1", session_id="s", type="UserPromptSubmit",
                          ts="2026-07-15T00:00:00+00:00", content="unique_token_xyz"))
    rows = db.search("unique_token_xyz")
    assert len(rows) == 1
    assert rows[0]["id"] == "evt-1"
    db.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_storage_sync.py -v`
Expected: `test_resyncing_same_event_does_not_duplicate_fts` FAILS with `assert 3 == 1`. `test_changing_event_content_reindexes` FAILS (old term still matches).

- [ ] **Step 3: Replace the FTS schema in `_init_schema`**

In `lib/storage.py`, replace the `events_fts` block (currently lines 196-202):

```sql
            -- FTS5 full-text index over events (EXTERNAL CONTENT).
            -- The FTS table reads its data from `events` via content_rowid=rowid
            -- and is maintained exclusively by the triggers below.
            --
            -- NEVER INSERT/UPDATE/DELETE events_fts from application code. The
            -- previous standalone table plus `INSERT OR REPLACE` silently
            -- duplicated every re-synced row (FTS5 has no PRIMARY KEY, so
            -- OR REPLACE degrades to a plain INSERT).
            CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
                content,
                content=events,
                content_rowid=rowid,
                tokenize='porter'
            );

            -- Guards mirror the old `if event.content:` behaviour: NULL and
            -- empty-string content were never indexed and must stay unindexed.
            CREATE TRIGGER IF NOT EXISTS events_fts_ai AFTER INSERT ON events
            WHEN new.content IS NOT NULL AND new.content != ''
            BEGIN
                INSERT INTO events_fts(rowid, content) VALUES (new.rowid, new.content);
            END;

            CREATE TRIGGER IF NOT EXISTS events_fts_ad AFTER DELETE ON events
            WHEN old.content IS NOT NULL AND old.content != ''
            BEGIN
                INSERT INTO events_fts(events_fts, rowid, content)
                VALUES('delete', old.rowid, old.content);
            END;

            CREATE TRIGGER IF NOT EXISTS events_fts_au_del AFTER UPDATE ON events
            WHEN old.content IS NOT NULL AND old.content != ''
            BEGIN
                INSERT INTO events_fts(events_fts, rowid, content)
                VALUES('delete', old.rowid, old.content);
            END;

            CREATE TRIGGER IF NOT EXISTS events_fts_au_ins AFTER UPDATE ON events
            WHEN new.content IS NOT NULL AND new.content != ''
            BEGIN
                INSERT INTO events_fts(rowid, content) VALUES (new.rowid, new.content);
            END;
```

Note: the `events` table must be created BEFORE `events_fts` in the script. It already is.

- [ ] **Step 4: Rewrite `insert_event` to DELETE + INSERT**

Replace `lib/storage.py:276-319` entirely:

```python
    def insert_event(self, event: Event) -> None:
        """Insert or replace an event. FTS5 is maintained by triggers.

        Uses explicit DELETE + INSERT rather than INSERT OR REPLACE. REPLACE
        assigns a NEW hidden rowid, which orphans the old external-content FTS
        entry, and REPLACE does not fire AFTER DELETE triggers unless
        recursive_triggers is ON for the connection. Measured: 3 re-syncs via
        REPLACE produce 3 FTS hits; via DELETE + INSERT, 1. DELETE + INSERT is
        correct regardless of per-connection pragmas.

        Do NOT touch events_fts here. Triggers own it.
        """
        with self._write_lock:
            self.conn.execute("DELETE FROM events WHERE id = ?", (event.id,))
            self.conn.execute(
                """
                INSERT INTO events
                (id, session_id, type, ts, agent_session_num, data, content,
                 persona, agent_id, tool_name, tool_input_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    event.id,
                    event.session_id,
                    event.type,
                    event.ts,
                    event.agent_session_num,
                    json.dumps(event.data),
                    event.content,
                    event.persona,
                    event.agent_id,
                    event.tool_name,
                    event.tool_input_hash,
                ),
            )
            self.conn.commit()
```

- [ ] **Step 5: Update both FTS joins to use rowid**

`lib/storage.py:333`, in `search()`, change:
```python
            FROM events_fts
            JOIN events e ON events_fts.event_id = e.id
```
to:
```python
            FROM events_fts
            JOIN events e ON e.rowid = events_fts.rowid
```

`lib/search.py:72`, make the identical change:
```python
            FROM events_fts
            JOIN events e ON e.rowid = events_fts.rowid
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_storage_sync.py tests/test_search.py -v`
Expected: all PASS.

- [ ] **Step 7: Run the whole suite for regressions**

Run: `uv run pytest -q`
Expected: the 5 pre-existing `TestLiveDataVerification` / `TestLiveTranscriptVerification` failures may remain (they assert against live machine state and are Phase 4's concern). No NEW failures. If `test_search.py` or `test_api.py` broke, a join was missed.

- [ ] **Step 8: Commit**

```bash
git add lib/storage.py lib/search.py tests/test_storage_sync.py
git commit -m "fix: convert events_fts to external-content FTS5

events_fts was standalone and insert_event used INSERT OR REPLACE. FTS5
has no PRIMARY KEY, so OR REPLACE degraded to plain INSERT and every
re-synced event silently duplicated. Backfill re-syncs by design, so this
blocks the backfill.

Triggers now own the index. insert_event uses explicit DELETE + INSERT:
REPLACE assigns a new hidden rowid (orphaning the old FTS entry) and does
not fire AFTER DELETE triggers without recursive_triggers=ON."
```

---

### Task 4: Migrate the live databases

**Files:**
- Create: `scripts/migrate_002_events_fts_external_content.py`

**Interfaces:**
- Consumes: the schema from Task 3.
- Produces: a CLI: `uv run scripts/migrate_002_events_fts_external_content.py [--dry-run] [--all]`.

- [ ] **Step 1: Write the migration**

Modelled on the fleet precedent at `../claude-meetings/scripts/migrate_003_fts_external_content.py`. Create `scripts/migrate_002_events_fts_external_content.py`:

```python
# /// script
# requires-python = ">=3.11"
# ///
"""One-time migration: convert events_fts to external-content FTS5.

events_fts was created standalone, and insert_event used INSERT OR REPLACE.
FTS5 tables have no PRIMARY KEY, so OR REPLACE degraded to a plain INSERT:
every re-synced event silently duplicated its FTS row. Live DBs are clean
today only because nothing has re-synced yet. Backfill re-syncs by design.

Converts to content=events, content_rowid=rowid with sync triggers, then
rebuilds from the existing rows. Safe: no data lives only in events_fts;
every indexed value is reconstructable from events.content.

Idempotent: re-running detects external content is already in place and exits.

Usage:
    uv run scripts/migrate_002_events_fts_external_content.py --dry-run
    uv run scripts/migrate_002_events_fts_external_content.py --all
"""

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

LOGGING_ROOT = Path.home() / ".claude" / "local" / "logging"

FTS_DDL = """
CREATE VIRTUAL TABLE events_fts USING fts5(
    content,
    content=events,
    content_rowid=rowid,
    tokenize='porter'
);
CREATE TRIGGER IF NOT EXISTS events_fts_ai AFTER INSERT ON events
WHEN new.content IS NOT NULL AND new.content != ''
BEGIN
    INSERT INTO events_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS events_fts_ad AFTER DELETE ON events
WHEN old.content IS NOT NULL AND old.content != ''
BEGIN
    INSERT INTO events_fts(events_fts, rowid, content) VALUES('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS events_fts_au_del AFTER UPDATE ON events
WHEN old.content IS NOT NULL AND old.content != ''
BEGIN
    INSERT INTO events_fts(events_fts, rowid, content) VALUES('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS events_fts_au_ins AFTER UPDATE ON events
WHEN new.content IS NOT NULL AND new.content != ''
BEGIN
    INSERT INTO events_fts(rowid, content) VALUES (new.rowid, new.content);
END;
"""


def is_external_content(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='events_fts'"
    ).fetchone()
    return bool(row) and "content=events" in row[0].replace("'", "").replace(" ", "")


def migrate(db_path: Path, dry_run: bool) -> str:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='events_fts'"
        ).fetchone():
            return "skip (no events_fts)"
        if is_external_content(conn):
            return "skip (already external content)"

        expected = conn.execute(
            "SELECT COUNT(*) FROM events WHERE content IS NOT NULL AND content != ''"
        ).fetchone()[0]
        if dry_run:
            return f"would migrate ({expected} rows to index)"

        backup = db_path.with_suffix(db_path.suffix + ".pre-migrate002")
        shutil.copy2(db_path, backup)

        conn.executescript("DROP TABLE events_fts;" + FTS_DDL)
        conn.execute("INSERT INTO events_fts(events_fts) VALUES('rebuild')")
        conn.execute("INSERT INTO events_fts(events_fts) VALUES('integrity-check')")
        conn.commit()

        indexed = conn.execute(
            "SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'e OR a OR i'"
        ).fetchone()[0]
        return f"migrated ({expected} rows expected, index rebuilt, integrity OK, backup {backup.name}, sample match {indexed})"
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true", help="migrate every per-project DB")
    ap.add_argument("--db", help="migrate a single DB path")
    args = ap.parse_args()

    if args.db:
        dbs = [Path(args.db)]
    elif args.all:
        dbs = sorted(LOGGING_ROOT.glob("*/db/logging.db"))
    else:
        print("specify --all or --db PATH")
        return 2

    for db in dbs:
        print(f"{db}: {migrate(db, args.dry_run)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Dry-run against every live DB**

Run: `uv run scripts/migrate_002_events_fts_external_content.py --all --dry-run`
Expected: one `would migrate (N rows to index)` line per project DB. Note the row counts; you will verify against them.

- [ ] **Step 3: Record the pre-migration search baseline**

```bash
sqlite3 ~/.claude/local/logging/-home-shawn/db/logging.db \
  "SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'session';"
```
Write the number down. Search results must not regress.

- [ ] **Step 4: Migrate for real**

Run: `uv run scripts/migrate_002_events_fts_external_content.py --all`
Expected: `migrated (...)` per DB, each reporting `integrity OK` and a `.pre-migrate002` backup.

- [ ] **Step 5: Verify search parity and idempotency**

```bash
sqlite3 ~/.claude/local/logging/-home-shawn/db/logging.db \
  "SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'session';"
uv run scripts/migrate_002_events_fts_external_content.py --all
```
Expected: the MATCH count equals Step 3's baseline, and the re-run prints `skip (already external content)` for every DB.

- [ ] **Step 6: Commit**

```bash
git add scripts/migrate_002_events_fts_external_content.py
git commit -m "feat: migration to external-content events_fts

Idempotent, backs up each DB first, rebuilds the index from events and
verifies integrity. Ported from claude-meetings migrate_003."
```

---

### Task 5: Fix the torn-line cursor (silent event loss)

`sync_session()` catches `JSONDecodeError` on a torn line, does `continue`, then advances `sync_state.last_position` to the pre-read file size anyway. That event is dropped from SQLite permanently.

There is a second, subtler bug in the same function: the file is opened in **text mode** but seeked to a **byte offset** from `stat().st_size`. For any non-ASCII content those are different units.

**Files:**
- Modify: `lib/storage.py:490-526` (`sync_session`)
- Modify: `tests/test_storage_sync.py`

**Interfaces:**
- Consumes: `StorageManager` from `lib/storage.py`.
- Produces: `StorageManager.sync_session(session_id: str) -> int` (unchanged signature; cursor semantics fixed).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_storage_sync.py`:

```python
def test_torn_line_is_retried_not_dropped(tmp_path):
    """A half-written trailing line must not advance the cursor past it."""
    sm = StorageManager(tmp_path / "logging")
    path = sm.jsonl.get_session_path("s1")
    path.parent.mkdir(parents=True, exist_ok=True)

    good = json.dumps({"id": "evt-1", "session_id": "s1", "type": "T",
                       "ts": "2026-07-15T00:00:00+00:00", "content": "first"})
    path.write_text(good + "\n" + '{"id": "evt-2", "session_id": "s1", "ty')

    assert sm.sync_session("s1") == 1
    cursor = sm.sqlite.get_sync_position("s1")
    assert cursor == len(good) + 1, "cursor must stop at the last complete line"

    # Writer completes the torn line; the event must now be picked up.
    rest = json.dumps({"id": "evt-2", "session_id": "s1", "type": "T",
                       "ts": "2026-07-15T00:00:01+00:00", "content": "second"})
    path.write_text(good + "\n" + rest + "\n")
    assert sm.sync_session("s1") == 1
    ids = {r[0] for r in sm.sqlite.conn.execute("SELECT id FROM events").fetchall()}
    assert ids == {"evt-1", "evt-2"}, "the torn line's event was lost forever"
    sm.close()


def test_cursor_is_byte_accurate_with_unicode(tmp_path):
    """Cursor is a byte offset; the file must be read in binary mode."""
    sm = StorageManager(tmp_path / "logging")
    path = sm.jsonl.get_session_path("s2")
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"id": "evt-1", "session_id": "s2", "type": "T",
                       "ts": "2026-07-15T00:00:00+00:00",
                       "content": "emoji 🔥 and accents éàü"}, ensure_ascii=False)
    path.write_text(line + "\n", encoding="utf-8")

    assert sm.sync_session("s2") == 1
    assert sm.sqlite.get_sync_position("s2") == path.stat().st_size
    assert sm.sync_session("s2") == 0, "a second sync must find nothing new"
    sm.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_storage_sync.py -k "torn or byte_accurate" -v`
Expected: `test_torn_line_is_retried_not_dropped` FAILS (cursor advanced past the torn line; `evt-2` never appears).

- [ ] **Step 3: Rewrite `sync_session`**

Replace `lib/storage.py:490-526`:

```python
    def sync_session(self, session_id: str) -> int:
        """Sync a session from JSONL to SQLite. Returns events synced.

        The cursor advances only to the end of the last COMPLETE, successfully
        parsed line. On a torn or unparseable line we stop and leave the cursor
        before it, so the next sync retries that exact byte range. The previous
        code did `continue` and then advanced the cursor to the pre-read file
        size, dropping the event from SQLite forever.

        Opened in binary mode on purpose: sync_state.last_position is a BYTE
        offset (compared against stat().st_size), and text-mode tell/seek do not
        return byte offsets for non-ASCII content.
        """
        last_pos = self.sqlite.get_sync_position(session_id)
        path = self.jsonl.get_session_path(session_id)
        if not path.exists():
            return 0
        if path.stat().st_size <= last_pos:
            return 0

        events_synced = 0
        first_event_data = None
        good_through = last_pos

        with open(path, "rb") as f:
            f.seek(last_pos)
            while True:
                raw = f.readline()
                if not raw:
                    break  # EOF
                if not raw.endswith(b"\n"):
                    break  # torn tail: writer mid-flight. Retry this range next sync.
                if raw.strip():
                    try:
                        data = json.loads(raw.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        break  # never advance past a line we could not parse
                    event = Event(**{k: v for k, v in data.items() if k in _EVENT_FIELDS})
                    self.sqlite.insert_event(event)
                    events_synced += 1
                    if first_event_data is None:
                        first_event_data = data
                good_through = f.tell()

        self.sqlite.update_sync_position(session_id, good_through)
        self._update_session_from_events(session_id, first_event_data)
        return events_synced
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_storage_sync.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: no new failures.

- [ ] **Step 6: Commit**

```bash
git add lib/storage.py tests/test_storage_sync.py
git commit -m "fix: sync cursor no longer skips torn JSONL lines

sync_session caught JSONDecodeError, did `continue`, then advanced
last_position to the pre-read file size anyway, dropping that event from
SQLite forever. The cursor now stops before any incomplete or unparseable
line so the next sync retries it.

Also reads in binary mode: last_position is a byte offset, and text-mode
seek/tell are not byte offsets for non-ASCII content."
```

---

### Task 6: One transaction per sync, and an honest busy_timeout

`sync_session()` currently commits `4+N` times: once per `insert_event`, once for `update_sync_position`, once for `insert_session`. Each commit independently contends for SQLite's single WAL writer slot. This is the main lever against SQLITE_BUSY before Task 8 multiplies write frequency.

Separately, `sqlite3.connect(timeout=10)` is silently overridden by `PRAGMA busy_timeout=5000`. The effective value is 5s while the code reads as 10s.

**Files:**
- Modify: `lib/storage.py:136-144` (`__init__`), `lib/storage.py:254-274` (`insert_session`), `insert_event`, `update_sync_position`, `sync_session`, `_update_session_from_events`

**Interfaces:**
- Consumes: `SQLiteStorage` from Task 3.
- Produces: `SQLiteStorage.transaction()` context manager. `insert_event(event, commit=True)`, `insert_session(session, commit=True)`, `update_sync_position(session_id, position, commit=True)` all gain a `commit` keyword defaulting to `True` so existing callers are unaffected.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_storage_sync.py`:

```python
def test_sync_session_uses_one_transaction(tmp_path, monkeypatch):
    """4+N commits per sync is the main SQLITE_BUSY driver under concurrency."""
    sm = StorageManager(tmp_path / "logging")
    path = sm.jsonl.get_session_path("s3")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"id": f"evt-{i}", "session_id": "s3", "type": "T",
                    "ts": f"2026-07-15T00:00:0{i}+00:00", "content": f"body {i}"})
        for i in range(5)
    ]
    path.write_text("\n".join(lines) + "\n")

    commits = {"n": 0}
    real_commit = sm.sqlite.conn.commit

    def counting_commit():
        commits["n"] += 1
        return real_commit()

    monkeypatch.setattr(sm.sqlite.conn, "commit", counting_commit)
    assert sm.sync_session("s3") == 5
    assert commits["n"] == 1, f"expected 1 commit for the whole sync, got {commits['n']}"
    sm.close()


def test_busy_timeout_is_what_the_code_claims(tmp_path):
    db = SQLiteStorage(tmp_path / "t.db")
    assert db.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
    db.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_storage_sync.py -k "one_transaction or busy_timeout" -v`
Expected: `test_sync_session_uses_one_transaction` FAILS with `expected 1 commit ... got 7`. `test_busy_timeout_is_what_the_code_claims` FAILS with `assert 5000 == 15000`.

- [ ] **Step 3: Make the lock reentrant and the timeout honest**

In `lib/storage.py`, replace lines 139-143:

```python
        # busy_timeout is the ONE source of truth for write-lock patience. The
        # connect(timeout=) kwarg sets the same underlying handler, so passing
        # both means whichever runs last wins. 15s: writes are frequent and
        # short (measured ~0.02ms), so a long retry budget costs nothing and
        # absorbs contention from concurrent hook processes.
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=15000")
        # RLock, not Lock: transaction() takes the lock and calls methods that
        # take it again. Note this only serialises threads WITHIN one process;
        # cross-process safety is SQLite's WAL writer lock plus busy_timeout.
        self._write_lock = threading.RLock()
```

- [ ] **Step 4: Add the transaction context manager**

Add to `SQLiteStorage`, after `_init_schema`. Add `from contextlib import contextmanager` to the imports at the top of the file.

```python
    @contextmanager
    def transaction(self):
        """Run a group of writes as one transaction.

        Collapses sync_session's 4+N commits into 1, which is the main lever
        against SQLITE_BUSY: every hook invocation is a separate process
        contending for SQLite's single WAL writer slot. Also closes the
        partial-crash window where events landed but the sync cursor did not.
        """
        with self._write_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
```

- [ ] **Step 5: Give the writers a `commit` flag**

In `insert_event`, change the signature to `def insert_event(self, event: Event, commit: bool = True) -> None:` and replace the trailing `self.conn.commit()` with:
```python
            if commit:
                self.conn.commit()
```

Apply the identical change to `insert_session` (line ~254) and `update_sync_position` (line ~430): add `commit: bool = True` to the signature and guard the trailing `self.conn.commit()`.

In `_update_session_from_events`, change the signature to `def _update_session_from_events(self, session_id: str, first_event_data: dict | None = None, commit: bool = True) -> None:` and pass it through: `self.sqlite.insert_session(session, commit=commit)`.

- [ ] **Step 6: Wrap `sync_session`'s writes in one transaction**

In `sync_session` (from Task 5), wrap the read loop and trailing writes:

```python
        with self.sqlite.transaction():
            with open(path, "rb") as f:
                f.seek(last_pos)
                while True:
                    raw = f.readline()
                    if not raw:
                        break
                    if not raw.endswith(b"\n"):
                        break
                    if raw.strip():
                        try:
                            data = json.loads(raw.decode("utf-8"))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            break
                        event = Event(**{k: v for k, v in data.items() if k in _EVENT_FIELDS})
                        self.sqlite.insert_event(event, commit=False)
                        events_synced += 1
                        if first_event_data is None:
                            first_event_data = data
                    good_through = f.tell()

            self.sqlite.update_sync_position(session_id, good_through, commit=False)
            self._update_session_from_events(session_id, first_event_data, commit=False)

        return events_synced
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_storage_sync.py -v && uv run pytest -q`
Expected: all of `test_storage_sync.py` PASSES; no new failures elsewhere.

- [ ] **Step 8: Commit**

```bash
git add lib/storage.py tests/test_storage_sync.py
git commit -m "perf: one transaction per sync_session; honest busy_timeout

sync_session committed 4+N times, each independently contending for
SQLite's single WAL writer slot. Every hook invocation is a separate
process, so this is the main SQLITE_BUSY driver before per-event sync
lands. Also closes the partial-crash window where events were durable but
the sync cursor was not.

connect(timeout=10) was silently overridden by PRAGMA busy_timeout=5000;
now one value, 15000."
```

---

### Task 7: Only trust cwd from SessionStart

`_update_session_from_events` prefers `cwd` from whatever event happened to be first in the sync window. Under per-event sync (Task 8) that is usually not the session's first event, and any event whose `data` dict happens to carry a `cwd` key can overwrite the session's real cwd.

**Files:**
- Modify: `lib/storage.py:528-577` (`_update_session_from_events`)
- Modify: `tests/test_storage_sync.py`

**Interfaces:**
- Consumes: `StorageManager` from Task 6.
- Produces: no signature change.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_storage_sync.py`:

```python
def test_cwd_only_taken_from_session_start(tmp_path):
    """A non-SessionStart event carrying a stray cwd must not set session cwd."""
    sm = StorageManager(tmp_path / "logging")
    path = sm.jsonl.get_session_path("s4")
    path.parent.mkdir(parents=True, exist_ok=True)
    start = json.dumps({"id": "evt-1", "session_id": "s4", "type": "SessionStart",
                        "ts": "2026-07-15T00:00:00+00:00",
                        "data": {"cwd": "/real/project"}, "content": "started"})
    path.write_text(start + "\n")
    sm.sync_session("s4")

    # A later Bash PreToolUse whose tool_input carries an unrelated cwd.
    tool = json.dumps({"id": "evt-2", "session_id": "s4", "type": "PreToolUse",
                       "ts": "2026-07-15T00:00:01+00:00",
                       "data": {"cwd": "/tmp/somewhere-else"}, "content": "bash"})
    with open(path, "a") as f:
        f.write(tool + "\n")
    sm.sync_session("s4")

    assert sm.sqlite.get_session("s4")["cwd"] == "/real/project"
    sm.close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_storage_sync.py -k cwd_only -v`
Expected: FAIL, `assert '/tmp/somewhere-else' == '/real/project'`.

- [ ] **Step 3: Guard the cwd extraction**

In `_update_session_from_events`, replace the cwd block (currently lines ~547-568):

```python
        # Only a SessionStart event's cwd is authoritative. Under per-event sync
        # `first_event_data` is merely the first event of THIS window, and other
        # event types can carry an unrelated `cwd` in their data payload.
        cwd = None
        if (
            first_event_data
            and first_event_data.get("type") == "SessionStart"
            and isinstance(first_event_data.get("data"), dict)
        ):
            cwd = first_event_data["data"].get("cwd")

        if not cwd:
            cursor = self.sqlite.conn.execute(
                """
                SELECT data FROM events
                WHERE session_id = ? AND type = 'SessionStart'
                ORDER BY ts LIMIT 1
            """,
                (session_id,),
            )
            data_row = cursor.fetchone()
            if data_row and data_row[0]:
                try:
                    cwd = json.loads(data_row[0]).get("cwd")
                except (json.JSONDecodeError, KeyError):
                    pass

        # Never regress a known cwd to NULL on a later partial sync.
        if not cwd:
            existing = self.sqlite.get_session(session_id)
            if existing:
                cwd = existing.get("cwd")
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_storage_sync.py -v && uv run pytest -q`
Expected: all PASS, no new failures.

- [ ] **Step 5: Commit**

```bash
git add lib/storage.py tests/test_storage_sync.py
git commit -m "fix: only trust cwd from SessionStart events

Under per-event sync the first event of a sync window is rarely the
session's first event, and other event types can carry an unrelated cwd
in their data payload. Also stops a later partial sync regressing a known
cwd to NULL."
```

---

### Task 8: Realtime inline sync

Sync currently fires only on `Stop`, `SubagentStop`, `PostCompact` (current session) and `SessionStart`, `SessionEnd` (`sync_all`). So the searchable index trails the JSONL by up to a turn. Measured against the real 88MB DB: an insert plus FTS plus commit is **0.020ms median, 0.082ms p95**, versus a **35-83ms** hook invocation. Inline sync costs ~0.05% of the hook. The batching buys nothing.

**Files:**
- Modify: `hooks/log_event.py:950-977`

**Interfaces:**
- Consumes: `StorageManager.sync_session` from Task 6.
- Produces: no new interfaces.

- [ ] **Step 1: Replace the turn-boundary sync block**

In `hooks/log_event.py`, replace the block at lines 950-977:

```python
    # Incremental SQLite sync on EVERY event: the index is current within
    # milliseconds of capture. Measured 0.02ms median against the real 88MB DB
    # versus a 35-83ms hook invocation, so this is ~0.05% of the hook path and
    # the previous turn-boundary batching bought nothing.
    #
    # Best-effort by design: JSONL is the source of truth and periodic
    # reconciliation repairs anything this misses, so a failure here must never
    # be fatal. sync_session is idempotent and resumes from its byte cursor, so
    # a dropped sync is picked up by the next event.
    try:
        from lib.storage import StorageManager

        sm = StorageManager(storage_path)
        try:
            sm.sync_session(session_id)
        finally:
            sm.close()
        write_heartbeat("logging")
    except Exception as e:
        log_error(e, f"SQLiteSync:{event_type}")

    # SessionStart still sweeps every session: catches anything a previous
    # process failed to sync (crash, SIGKILL, SQLITE_BUSY) without waiting for
    # the reconciler.
    if event_type == "SessionStart":
        try:
            from lib.storage import StorageManager

            sm = StorageManager(storage_path)
            try:
                sm.sync_all()
            finally:
                sm.close()
        except Exception as e:
            log_error(e, f"SQLiteSyncAll:{event_type}")
```

- [ ] **Step 2: Verify capture end-to-end with a real session**

```bash
DB=~/.claude/local/logging/-home-shawn/db/logging.db
BEFORE=$(sqlite3 $DB "SELECT COUNT(*) FROM events;")
claude -p "reply with exactly: pong" --model claude-haiku-4-5-20251001 < /dev/null
sleep 3
AFTER=$(sqlite3 $DB "SELECT COUNT(*) FROM events;")
echo "events: $BEFORE -> $AFTER"
sqlite3 $DB "SELECT type, ts FROM events ORDER BY ts DESC LIMIT 5;"
```
Expected: the count increases; the newest rows are from seconds ago.

- [ ] **Step 3: Verify no FTS duplication crept in under real load**

```bash
sqlite3 ~/.claude/local/logging/-home-shawn/db/logging.db \
  "INSERT INTO events_fts(events_fts) VALUES('integrity-check');" && echo "FTS integrity OK"
```
Expected: `FTS integrity OK`. Per-event sync re-reads ranges far more often than turn-boundary sync did, so this is the real-world proof Task 3 holds.

- [ ] **Step 4: Confirm the hook is still silent**

```bash
tail -5 ~/.claude/local/logging/-home-shawn/errors.log 2>/dev/null || echo "no errors.log (good)"
```
Expected: no new entries. Any `SQLiteSync` errors here mean contention; Task 10 measures it.

- [ ] **Step 5: Commit**

```bash
git add hooks/log_event.py
git commit -m "feat: sync SQLite inline on every event

The index was up to a turn behind because sync only fired at turn
boundaries. Measured 0.02ms median against the real 88MB DB versus a
35-83ms hook, so batching bought nothing. Stays best-effort: JSONL is the
source of truth and reconciliation repairs the rest."
```

---

### Task 9: Stop re-parsing the whole session on every event

`get_agent_session_num()` re-reads and re-parses the entire session JSONL on every hook invocation to count `compact`/`clear` markers. Measured: **35ms on an empty session, 83ms on a 14.7MB one**. Cost grows without bound as a session runs, and it lengthens the window each process holds resources during per-event sync.

**Files:**
- Modify: `hooks/log_event.py:166-195` (`get_agent_session_num`)
- Create: `tests/test_agent_session_num.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `get_agent_session_num(session_path: Path) -> int` (unchanged signature; now O(1) amortised via a sidecar counter file).

- [ ] **Step 1: Read the current implementation**

Run: `sed -n '160,200p' hooks/log_event.py`

Understand exactly what it counts before changing it. `tests/test_process_event.py` covers false-positive agent-session counting (a CHANGELOG-noted past bug); do not regress it.

- [ ] **Step 2: Write the characterisation test**

Create `tests/test_agent_session_num.py`. This pins current behaviour BEFORE optimising, so the cache cannot silently change semantics:

```python
"""Characterisation tests for get_agent_session_num.

Written before optimising it from O(session length) to O(1) amortised, so the
cache cannot change what the function actually counts.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hooks.log_event import get_agent_session_num  # noqa: E402


def _write(path, events):
    path.write_text("".join(json.dumps(e) + "\n" for e in events))


def test_no_markers_is_zero(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [{"type": "UserPromptSubmit", "content": "hi"}])
    assert get_agent_session_num(p) == 0


def test_missing_file_is_zero(tmp_path):
    assert get_agent_session_num(tmp_path / "nope.jsonl") == 0


def test_counter_matches_full_scan_after_appends(tmp_path):
    """The cached counter must agree with a from-scratch scan."""
    p = tmp_path / "s.jsonl"
    _write(p, [{"type": "UserPromptSubmit", "content": "one"}])
    first = get_agent_session_num(p)
    with open(p, "a") as f:
        f.write(json.dumps({"type": "UserPromptSubmit", "content": "two"}) + "\n")
    assert get_agent_session_num(p) == first
```

- [ ] **Step 3: Run to establish the baseline**

Run: `uv run pytest tests/test_agent_session_num.py -v`
Expected: PASS against the current implementation. If any fail, the test encodes the wrong expectation. Fix the test, not the code, before proceeding.

- [ ] **Step 4: Add a sidecar counter cache**

Modify `get_agent_session_num` in `hooks/log_event.py` to cache `(file_size, count)` in a sidecar next to the session JSONL, rescanning only the bytes appended since the last call:

```python
def get_agent_session_num(session_path: Path) -> int:
    """Count agent-session boundaries (compact/clear markers) in a session.

    Cached in a sidecar to keep this O(bytes appended since last call) rather
    than O(whole session). It previously re-parsed the entire JSONL on every
    hook invocation: 35ms on an empty session, 83ms on a 14.7MB one, growing
    for the life of the session.

    The cache is derived state and always safe to delete: a missing or stale
    sidecar just triggers a full rescan.
    """
    if not session_path.exists():
        return 0

    cache_path = session_path.with_suffix(".agentnum")
    size = session_path.stat().st_size
    start, count = 0, 0

    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if cached["size"] <= size:  # file only ever grows; shrink => rescan
                start, count = cached["size"], cached["count"]
        except (json.JSONDecodeError, KeyError, TypeError):
            start, count = 0, 0  # unreadable cache: rescan from scratch

    with open(session_path, "rb") as f:
        f.seek(start)
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # torn tail: do not count it, and do not cache past it
            if not raw.strip():
                continue
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                break
            if _is_agent_session_boundary(data):
                count += 1
            start = f.tell()

    try:
        cache_path.write_text(json.dumps({"size": start, "count": count}))
    except OSError:
        pass  # cache is an optimisation; never fail the hook over it

    return count
```

Extract the existing marker condition from the old loop body into `_is_agent_session_boundary(data: dict) -> bool` and keep its logic **byte-identical**. Do not "improve" it; `tests/test_process_event.py` pins a past false-positive fix.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_agent_session_num.py tests/test_process_event.py -v`
Expected: all PASS.

- [ ] **Step 6: Measure the improvement**

```bash
python3 - <<'EOF'
import subprocess, time, json, os
ROOT = "/home/shawn/Workspace/legion-plugins/plugins/claude-logging"
SESS = os.path.expanduser("~/.claude/local/logging/-home-shawn/sessions")
big = max((os.path.join(SESS, f) for f in os.listdir(SESS) if f.endswith(".jsonl")), key=os.path.getsize)
sid = os.path.basename(big)[:-6]
payload = json.dumps({"session_id": sid, "transcript_path": "/tmp/x.jsonl", "cwd": "/home/shawn",
                      "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "true"}})
ts = []
for _ in range(3):
    s = time.perf_counter()
    subprocess.run(["uv", "run", f"{ROOT}/hooks/log_event.py", "-e", "PreToolUse"],
                   input=payload, capture_output=True, text=True)
    ts.append((time.perf_counter() - s) * 1000)
print(f"{os.path.getsize(big)/1e6:.1f} MB session: {min(ts):.0f} ms  (was ~83 ms)")
EOF
```
Expected: materially below 83ms, near the ~35ms empty-session floor. **Then remove the probe events this appends:**
```bash
python3 - <<'EOF'
import json, os
SESS = os.path.expanduser("~/.claude/local/logging/-home-shawn/sessions")
big = max((os.path.join(SESS, f) for f in os.listdir(SESS) if f.endswith(".jsonl")), key=os.path.getsize)
keep, removed = [], 0
for line in open(big):
    try:
        d = json.loads(line)
    except Exception:
        keep.append(line); continue
    ti = d.get("data", {}).get("tool_input")
    if d.get("type") == "PreToolUse" and isinstance(ti, dict) and ti.get("command") == "true":
        removed += 1; continue
    keep.append(line)
open(big, "w").writelines(keep)
print(f"removed {removed} probe lines")
EOF
```

- [ ] **Step 7: Add the sidecar to .gitignore and commit**

```bash
echo "*.agentnum" >> .gitignore
git add hooks/log_event.py tests/test_agent_session_num.py .gitignore
git commit -m "perf: cache agent-session counter in a sidecar

get_agent_session_num re-parsed the entire session JSONL on every hook
invocation: 35ms empty, 83ms at 14.7MB, growing for the session's life.
Now rescans only bytes appended since the last call. The sidecar is
derived state and safe to delete at any time."
```

---

### Task 10: Prove concurrency is safe

The audit flagged that per-event sync across many concurrent hook processes could cause SQLITE_BUSY storms, and proposed a single-writer daemon. The spec rejects that as premature. This task is the evidence for that call: if BUSY rates are non-trivial, the daemon decision reopens.

**Files:**
- Create: `tests/test_concurrency.py`

**Interfaces:**
- Consumes: `SQLiteStorage` from Task 6.
- Produces: nothing.

- [ ] **Step 1: Write the concurrency test**

Create `tests/test_concurrency.py`:

```python
"""Proves per-event inline sync is safe across concurrent hook processes.

Every hook invocation is a separate `uv run` process, all writing one
logging.db. The threading.Lock in SQLiteStorage does nothing across processes;
the only real serialisation is SQLite's single WAL writer slot plus
busy_timeout. If this test shows meaningful BUSY rates, the single-writer
daemon rejected in the spec needs reconsidering.
"""

import multiprocessing
import sqlite3
import uuid
from pathlib import Path

import pytest

from lib.storage import Event, SQLiteStorage

WORKERS = 8
EVENTS_PER_WORKER = 40


def _worker(db_path_str, n):
    db = SQLiteStorage(Path(db_path_str))
    busy = 0
    for _ in range(n):
        try:
            db.insert_event(Event(
                id=f"evt_{uuid.uuid4().hex[:12]}", session_id="shared",
                type="PostToolUse", ts="2026-07-15T00:00:00+00:00",
                content="a realistic tool result payload of moderate length",
            ))
        except sqlite3.OperationalError:
            busy += 1
    db.close()
    return busy


def test_concurrent_writers_do_not_hit_busy(tmp_path):
    db_path = tmp_path / "logging.db"
    SQLiteStorage(db_path).close()  # create schema once up front

    with multiprocessing.Pool(WORKERS) as pool:
        busies = pool.starmap(_worker, [(str(db_path), EVENTS_PER_WORKER)] * WORKERS)

    total_busy = sum(busies)
    conn = sqlite3.connect(str(db_path))
    written = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.execute("INSERT INTO events_fts(events_fts) VALUES('integrity-check')")
    conn.close()

    assert total_busy == 0, f"{total_busy} SQLITE_BUSY across {WORKERS} writers"
    assert written == WORKERS * EVENTS_PER_WORKER, f"lost writes: {written}"
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_concurrency.py -v`
Expected: PASS, zero BUSY, no lost writes, FTS integrity intact under concurrent writers.

If it FAILS: do not paper over it by raising `busy_timeout`. Record the BUSY rate, stop, and reopen the single-writer daemon decision in the spec. That is a design change, not a tuning problem.

- [ ] **Step 3: Commit**

```bash
git add tests/test_concurrency.py
git commit -m "test: prove per-event sync is safe across concurrent writers

Evidence for the spec's rejection of a single-writer daemon. Every hook
invocation is its own process; the in-process lock does nothing across
them. If this ever fails, the daemon decision reopens."
```

---

### Task 11: Full verification

- [ ] **Step 1: Run the whole suite**

Run: `uv run pytest -q`
Expected: everything passes except the 5 pre-existing `TestLiveDataVerification` / `TestLiveTranscriptVerification` failures, which assert live machine state (empty `session_summaries`, transcript counts) and are Phase 4's job. **Confirm the count is exactly 5 and that they are the same 5.** Any other failure is a regression from this plan.

- [ ] **Step 2: Verify live capture end-to-end one more time**

```bash
DB=~/.claude/local/logging/-home-shawn/db/logging.db
BEFORE=$(sqlite3 $DB "SELECT COUNT(*) FROM events;")
claude -p "reply with exactly: verified" --model claude-haiku-4-5-20251001 < /dev/null
sleep 3
echo "events: $BEFORE -> $(sqlite3 $DB 'SELECT COUNT(*) FROM events;')"
echo "max ts: $(sqlite3 $DB 'SELECT MAX(ts) FROM events;')"
sqlite3 $DB "INSERT INTO events_fts(events_fts) VALUES('integrity-check');" && echo "FTS integrity OK"
uv run pytest tests/test_plugin_registration.py -v
```
Expected: events increase, `max ts` is seconds old, FTS integrity OK, registration tests pass.

- [ ] **Step 3: Confirm search works against the migrated live index**

```bash
uv run python3 -c "
from pathlib import Path
from lib.storage import StorageManager
sm = StorageManager(Path.home() / '.claude/local/logging/-home-shawn')
for r in sm.search('session', limit=3):
    print(r['type'], r['ts'], (r['content'] or '')[:60])
sm.close()
"
```
Expected: real results. This exercises the rowid join against the migrated production index.

- [ ] **Step 4: Report**

Write down: pytest summary, live event delta, FTS integrity, hook latency before/after Task 9, and the concurrency BUSY rate. These are the inputs to the Phase 4-5 plan.

---

## Self-Review

**Spec coverage (Phases 1-3):**
- Phase 1.1 duplicate manifest -> Task 2. 1.3 registration test -> Task 1. 1.4 CI gate -> Task 1 Step 3. 1.5 schedule live-data tests -> **deferred to the Phase 4-5 plan**, since those 5 tests fail on data the backfill produces; scheduling them now would wire a permanently-red alarm.
- Phase 1.2 name mismatch (`"name": "logging"` vs directory `claude-logging`) -> **deliberately not done.** It works today (debug confirms hooks register under `logging`), and `enabledPlugins` keys on `claude-logging@legion-plugins`. Renaming risks breaking enablement to fix a cosmetic inconsistency. Recorded in the spec's open items instead.
- Phase 2.1 FTS5 -> Tasks 3, 4. 2.2 torn line -> Task 5. 2.3 single transaction -> Task 6. 2.4 busy_timeout -> Task 6. 2.5 cwd guard -> Task 7.
- Phase 3.1 inline sync -> Task 8. 3.2 non-fatal failures -> Task 8 Step 1. 3.3 concurrency test -> Task 10. 3.4 O(n) hot path -> Task 9.

**Deviation from spec, decided by measurement:** the spec (following the audit) proposed external-content FTS5 keeping `event_id UNINDEXED`. That is not possible: external content populates by reading columns **from the base table**, and `events` has `id`, not `event_id`. The FTS table therefore carries only `content`, and both joins move to `rowid`. The spec's `INSERT OR REPLACE` assumption is also wrong, measured: REPLACE assigns a new rowid and skips AFTER DELETE triggers, yielding 3 hits per 3 re-syncs. Task 3 uses DELETE + INSERT.

**Placeholder scan:** none. Every code step carries complete code; every command has expected output.

**Type consistency:** `insert_event(event, commit=True)`, `insert_session(session, commit=True)`, `update_sync_position(session_id, position, commit=True)`, `_update_session_from_events(session_id, first_event_data=None, commit=True)`, `transaction()`, `get_agent_session_num(session_path) -> int`, `_is_agent_session_boundary(data) -> bool`. Task 6 introduces the `commit` kwarg; Tasks 5 and 6 both edit `sync_session`, and Task 6 shows the merged final form.

# Rollup Completeness Reconcile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `_index/index.db` a provably complete projection of the per-project logging DBs by adding a true event_id anti-join reconcile pass, used both as a one-shot recovery and as the daemon's 5-minute safety net.

**Architecture:** One new function `reconcile_project()` in `rollup_index.py` (count fast-path → on drift, anti-join missing `event_id`s → `INSERT OR IGNORE` into `events_index` + `events_index_fts`, update truthful `rollup_state.event_count`). Two call sites: a `--reconcile` CLI flag and the daemon's `_run_reconcile()`. The hot-path watermark ingest is left untouched for latency; correctness comes from reconcile. Index completeness % is computed during the daemon sweep and surfaced via `daemon-health.json` → accessor `stats()`/`healthz()`.

**Tech Stack:** Python 3, stdlib `sqlite3`, `pytest` (run via `uv run --extra dev pytest`), asyncio daemon (`watchfiles`).

---

## Background facts (engineer has zero context — read these)

- **Per-project source DBs:** `~/.claude/local/logging/<slug>/db/logging.db`, table `events(id TEXT PK, session_id, type, ts, agent_session_num, data JSON, content, persona)`. These are the source of truth and are NEVER mutated by this work.
- **Cross-project index DB:** `~/.claude/local/logging/_index/index.db`. Tables:
  - `events_index(event_id TEXT PK, project_slug, session_id, type, ts, persona, content_preview, has_full_content)`
  - `events_index_fts` — FTS5 mirror `(event_id, project_slug, session_id, type, persona, content_preview)`, `tokenize='porter'`. **No UNIQUE constraint** — only insert rows for events not already indexed.
  - `rollup_state(project_slug PK, last_event_ts, last_synced_at, event_count, schema_version)`
- **The bug:** `rollup_project()` (in `scripts/v2/rollup_index.py`) ingests `WHERE ts > last_event_ts`. Out-of-order writes with `ts < watermark` are skipped forever. The daemon's 5-min `_run_reconcile()` calls the same watermark function, so it cannot recover them.
- **Projection rule (must match `rollup_project` exactly):** `content_preview = (content or "")[:200]`; `has_full_content = 1 if content and len(content) > 200 else 0`. The constant `CONTENT_PREVIEW_LEN = 200` already exists in `rollup_index.py`.
- **`event_id` is globally unique** (`evt_<hash>`) and the index PK — anti-join on `event_id` alone is correct.
- **Run tests with:** `uv run --extra dev pytest <path> -v` (CWD = plugin root `/home/shawn/.claude/plugins/local/legion-plugins/plugins/claude-logging`).
- **Branch:** already on `fix/rollup-completeness-reconcile`.

## File structure

- **Modify** `scripts/v2/rollup_index.py` — add `_index_count()`, `_upsert_event_count()`, `reconcile_project()`, `run_reconcile_all()`, and a `--reconcile` / `--dry-run` CLI path. (Task 1, 2)
- **Modify** `scripts/v2/rollup_daemon.py` — add `_reconcile_one()`, switch `_run_reconcile()` to it, accumulate completeness counters, write them in `_write_health()`. (Task 3, 4a)
- **Modify** `web/logging_accessor.py` — `stats()` + `healthz()` read `_index/daemon-health.json` completeness block. (Task 4b)
- **Create** `tests/test_rollup_reconcile.py` — unit + regression tests. (Task 1, 2)

---

### Task 1: `reconcile_project()` core function + tests

**Files:**
- Modify: `scripts/v2/rollup_index.py` (add functions after `rollup_project`, ~line 113)
- Test: `tests/test_rollup_reconcile.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rollup_reconcile.py`:

```python
"""task-4134 — true reconcile pass recovers sub-watermark / out-of-order events."""
import sqlite3
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts" / "v2"))

import rollup_index  # noqa: E402

INDEX_DDL = (PLUGIN_ROOT / "scripts" / "v2" / "init_cross_project_index.sql").read_text()


def _make_source(path: Path, events: list[dict]) -> None:
    """Create a per-project source logging.db with the given events."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE events (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, "
        "ts TIMESTAMP, agent_session_num INTEGER DEFAULT 0, data JSON, "
        "content TEXT, persona TEXT)"
    )
    con.executemany(
        "INSERT INTO events (id, session_id, type, ts, content, persona) "
        "VALUES (:id, :session_id, :type, :ts, :content, :persona)",
        events,
    )
    con.commit()
    con.close()


def _make_index(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(INDEX_DDL)
    con.commit()
    return con


def _ev(eid, ts, content="hello", typ="UserPromptSubmit", persona=None):
    return {"id": eid, "session_id": "s1", "type": typ, "ts": ts,
            "content": content, "persona": persona}


def test_reconcile_recovers_sub_watermark_event(tmp_path):
    """The exact bug: an event with ts < watermark is skipped by rollup_project
    but recovered by reconcile_project."""
    src = tmp_path / "proj" / "db" / "logging.db"
    _make_source(src, [
        _ev("evt-1", "2026-06-14T01:00:00+00:00"),
        _ev("evt-2", "2026-06-14T02:00:00+00:00"),
    ])
    idx = _make_index(tmp_path / "index.db")

    # Initial watermark rollup pulls both events.
    inserted, max_ts = rollup_index.rollup_project(idx, "proj", src)
    rollup_index.update_rollup_state(idx, "proj", max_ts, inserted)
    idx.commit()
    assert idx.execute("SELECT COUNT(*) FROM events_index").fetchone()[0] == 2

    # A late event arrives with ts BELOW the current watermark (02:00).
    scon = sqlite3.connect(src)
    scon.execute(
        "INSERT INTO events (id, session_id, type, ts, content) "
        "VALUES ('evt-0', 's1', 'UserPromptSubmit', '2026-06-14T00:30:00+00:00', 'late')"
    )
    scon.commit()
    scon.close()

    # rollup_project (watermark) MISSES it — this is the bug.
    inserted2, _ = rollup_index.rollup_project(idx, "proj", src)
    idx.commit()
    assert inserted2 == 0
    assert idx.execute("SELECT COUNT(*) FROM events_index").fetchone()[0] == 2

    # reconcile_project RECOVERS it.
    recovered, idx_count = rollup_index.reconcile_project(idx, "proj", src)
    idx.commit()
    assert recovered == 1
    assert idx_count == 3
    assert idx.execute(
        "SELECT content_preview FROM events_index WHERE event_id='evt-0'"
    ).fetchone()[0] == "late"


def test_reconcile_idempotent(tmp_path):
    src = tmp_path / "proj" / "db" / "logging.db"
    _make_source(src, [_ev("evt-1", "2026-06-14T01:00:00+00:00")])
    idx = _make_index(tmp_path / "index.db")

    first, _ = rollup_index.reconcile_project(idx, "proj", src)
    idx.commit()
    assert first == 1
    second, _ = rollup_index.reconcile_project(idx, "proj", src)
    idx.commit()
    assert second == 0


def test_reconcile_fast_path_no_drift(tmp_path):
    """Matching counts → 0 inserts, event_count made truthful."""
    src = tmp_path / "proj" / "db" / "logging.db"
    _make_source(src, [_ev("evt-1", "2026-06-14T01:00:00+00:00")])
    idx = _make_index(tmp_path / "index.db")
    rollup_index.reconcile_project(idx, "proj", src)
    idx.commit()

    inserted, idx_count = rollup_index.reconcile_project(idx, "proj", src)
    idx.commit()
    assert inserted == 0
    assert idx_count == 1
    assert idx.execute(
        "SELECT event_count FROM rollup_state WHERE project_slug='proj'"
    ).fetchone()[0] == 1


def test_reconcile_recovered_event_is_searchable(tmp_path):
    src = tmp_path / "proj" / "db" / "logging.db"
    _make_source(src, [_ev("evt-9", "2026-06-14T01:00:00+00:00", content="findme zebra")])
    idx = _make_index(tmp_path / "index.db")
    rollup_index.reconcile_project(idx, "proj", src)
    idx.commit()
    hit = idx.execute(
        "SELECT event_id FROM events_index_fts WHERE events_index_fts MATCH 'zebra'"
    ).fetchone()
    assert hit is not None and hit[0] == "evt-9"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_rollup_reconcile.py -v`
Expected: FAIL — `AttributeError: module 'rollup_index' has no attribute 'reconcile_project'`

- [ ] **Step 3: Implement `reconcile_project` and helpers**

In `scripts/v2/rollup_index.py`, insert after `rollup_project()` (before `update_rollup_state`, ~line 113):

```python
def _index_count(idx_con: sqlite3.Connection, slug: str) -> int:
    return idx_con.execute(
        "SELECT COUNT(*) FROM events_index WHERE project_slug = ?", (slug,)
    ).fetchone()[0]


def _upsert_event_count(idx_con: sqlite3.Connection, slug: str, count: int) -> None:
    """Set rollup_state.event_count to the true index count WITHOUT touching
    last_event_ts (the hot-path watermark)."""
    idx_con.execute(
        "INSERT INTO rollup_state (project_slug, event_count, schema_version) "
        "VALUES (?, ?, 1) "
        "ON CONFLICT(project_slug) DO UPDATE SET event_count = excluded.event_count",
        (slug, count),
    )


def reconcile_project(
    idx_con: sqlite3.Connection, slug: str, db_path: Path
) -> tuple[int, int]:
    """True completeness pass for one shard, keyed on event_id (not ts).

    Fast path: if source COUNT == index COUNT for this slug, no work.
    On drift: anti-join source events by event_id, INSERT OR IGNORE the missing
    rows into events_index + events_index_fts (same projection as rollup_project).

    Returns (inserted, index_count_for_slug).
    """
    proj_con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    proj_con.row_factory = sqlite3.Row
    try:
        src_count = proj_con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        idx_count = _index_count(idx_con, slug)

        if src_count == idx_count:
            _upsert_event_count(idx_con, slug, idx_count)
            return 0, idx_count

        existing = {
            r[0] for r in idx_con.execute(
                "SELECT event_id FROM events_index WHERE project_slug = ?", (slug,)
            )
        }
        new_rows = []
        fts_rows = []
        for row in proj_con.execute(
            "SELECT id, session_id, type, ts, persona, content FROM events"
        ):
            if row["id"] in existing:
                continue
            content_preview = (row["content"] or "")[:CONTENT_PREVIEW_LEN]
            has_full = 1 if (row["content"] and len(row["content"]) > CONTENT_PREVIEW_LEN) else 0
            new_rows.append((
                row["id"], slug, row["session_id"], row["type"], row["ts"],
                row["persona"], content_preview, has_full,
            ))
            fts_rows.append((
                row["id"], slug, row["session_id"], row["type"],
                row["persona"] or "", content_preview,
            ))
    finally:
        proj_con.close()

    if new_rows:
        idx_con.executemany(
            "INSERT OR IGNORE INTO events_index "
            "(event_id, project_slug, session_id, type, ts, persona, content_preview, has_full_content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            new_rows,
        )
        idx_con.executemany(
            "INSERT INTO events_index_fts "
            "(event_id, project_slug, session_id, type, persona, content_preview) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            fts_rows,
        )

    final_count = _index_count(idx_con, slug)
    _upsert_event_count(idx_con, slug, final_count)
    return len(new_rows), final_count
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_rollup_reconcile.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/v2/rollup_index.py tests/test_rollup_reconcile.py
git commit -m "feat: reconcile_project() event_id anti-join completeness pass (task-4134)"
```

---

### Task 2: `--reconcile` CLI one-shot + dry-run

**Files:**
- Modify: `scripts/v2/rollup_index.py` (add `run_reconcile_all()`, extend `main()`)
- Test: `tests/test_rollup_reconcile.py` (add)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rollup_reconcile.py`:

```python
def test_run_reconcile_all_over_shards(tmp_path, monkeypatch):
    root = tmp_path / "logging"
    (root / "_index").mkdir(parents=True)
    # two shards, each with one event
    for slug in ("projA", "projB"):
        _make_source(root / slug / "db" / "logging.db",
                     [_ev(f"evt-{slug}", "2026-06-14T01:00:00+00:00")])
    idx_path = root / "_index" / "index.db"
    _make_index(idx_path).close()

    monkeypatch.setattr(rollup_index, "LOGGING_ROOT", root)
    monkeypatch.setattr(rollup_index, "INDEX_DB", idx_path)

    total = rollup_index.run_reconcile_all(dry_run=False, quiet=True)
    assert total == 2

    con = sqlite3.connect(idx_path)
    assert con.execute("SELECT COUNT(*) FROM events_index").fetchone()[0] == 2
    con.close()


def test_run_reconcile_all_dry_run_writes_nothing(tmp_path, monkeypatch):
    root = tmp_path / "logging"
    (root / "_index").mkdir(parents=True)
    _make_source(root / "projA" / "db" / "logging.db",
                 [_ev("evt-a", "2026-06-14T01:00:00+00:00")])
    idx_path = root / "_index" / "index.db"
    _make_index(idx_path).close()
    monkeypatch.setattr(rollup_index, "LOGGING_ROOT", root)
    monkeypatch.setattr(rollup_index, "INDEX_DB", idx_path)

    would = rollup_index.run_reconcile_all(dry_run=True, quiet=True)
    assert would == 1
    con = sqlite3.connect(idx_path)
    assert con.execute("SELECT COUNT(*) FROM events_index").fetchone()[0] == 0
    con.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_rollup_reconcile.py -k reconcile_all -v`
Expected: FAIL — `AttributeError: ... 'run_reconcile_all'`

- [ ] **Step 3: Implement `run_reconcile_all` and CLI wiring**

In `scripts/v2/rollup_index.py`, add after `reconcile_project()`:

```python
def run_reconcile_all(dry_run: bool = False, quiet: bool = False) -> int:
    """Reconcile every shard. Returns total events recovered (or would-recover
    in dry_run). For dry_run, computes per-shard (source - index) drift without
    writing."""
    idx_con = sqlite3.connect(INDEX_DB, timeout=30.0)
    total = 0
    dbs = discover_dbs()
    for i, (slug, db) in enumerate(dbs, 1):
        try:
            if dry_run:
                proj = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30.0)
                src_count = proj.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                proj.close()
                drift = max(0, src_count - _index_count(idx_con, slug))
                if drift and not quiet:
                    print(f"  {i:3}/{len(dbs)} DRIFT {drift:6,} {slug}")
                total += drift
            else:
                recovered, _ = reconcile_project(idx_con, slug, db)
                idx_con.commit()
                if recovered and not quiet:
                    print(f"  {i:3}/{len(dbs)} +{recovered:6,} {slug}")
                total += recovered
        except Exception as e:
            print(f"  {i:3}/{len(dbs)} ERROR {slug}: {type(e).__name__}: {e}")
    idx_con.close()
    verb = "would recover" if dry_run else "recovered"
    print(f"=== RECONCILE: {verb} {total:,} events across {len(dbs)} shards ===")
    return total
```

Then in `main()`, add the flags (after the existing `--quiet` arg, ~line 146):

```python
    parser.add_argument("--reconcile", action="store_true",
                        help="Run a true event_id anti-join completeness pass over all shards")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --reconcile: report drift without writing")
```

And at the top of `main()`'s body, right after `args = parser.parse_args()` and the `INDEX_DB.exists()` guard (after ~line 151), short-circuit:

```python
    if args.reconcile:
        return 0 if run_reconcile_all(dry_run=args.dry_run, quiet=args.quiet) >= 0 else 1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_rollup_reconcile.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/v2/rollup_index.py tests/test_rollup_reconcile.py
git commit -m "feat: rollup_index.py --reconcile one-shot + --dry-run (task-4134)"
```

---

### Task 3: Daemon uses reconcile_project for its safety-net pass

**Files:**
- Modify: `scripts/v2/rollup_daemon.py` (import, add `_reconcile_one`, change `_run_reconcile`)

- [ ] **Step 1: Add `reconcile_project` to the daemon's import**

In `scripts/v2/rollup_daemon.py`, extend the existing import block (lines 58-66) to include `reconcile_project`:

```python
from rollup_index import (  # noqa: E402
    HOSTNAME,
    INDEX_DB,
    LOGGING_ROOT,
    discover_dbs,
    reconcile_project,
    rollup_project,
    update_hostname_state,
    update_rollup_state,
)
```

- [ ] **Step 2: Add `_reconcile_one` next to `_rollup_one`**

In `scripts/v2/rollup_daemon.py`, add this method to `RollupDaemon` immediately after `_rollup_one` (after ~line 202):

```python
    def _reconcile_one(self, slug: str, db_path: Path) -> tuple[int, int]:
        """True completeness reconcile for one shard, with degraded-shard guard.
        Returns (inserted, index_count)."""
        con = self._open_index()
        try:
            inserted, idx_count = reconcile_project(con, slug, db_path)
            con.commit()
            if slug in self.degraded:
                logging.info("shard recovered: %s", slug)
                self.degraded.pop(slug, None)
            return inserted, idx_count
        except sqlite3.OperationalError as e:
            msg = str(e)
            if self.degraded.get(slug) != msg:
                logging.warning("shard degraded (reconcile): %s -> %s", slug, msg)
                self.degraded[slug] = msg
            return 0, 0
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if self.degraded.get(slug) != msg:
                logging.exception("shard error (reconcile): %s -> %s", slug, msg)
                self.degraded[slug] = msg
            return 0, 0
```

- [ ] **Step 3: Switch `_run_reconcile` to use it + track completeness**

In `scripts/v2/rollup_daemon.py`, replace the body of `_run_reconcile` (lines 286-315) with:

```python
    async def _run_reconcile(self) -> None:
        t0 = time.time()
        dbs = await asyncio.to_thread(discover_dbs)
        total_inserted = 0
        index_total = 0
        source_total = 0
        for slug, db in dbs:
            inserted, idx_count = await asyncio.to_thread(self._reconcile_one, slug, db)
            total_inserted += inserted
            index_total += idx_count
            try:
                src_count = await asyncio.to_thread(_shard_event_count, db)
            except Exception:
                src_count = idx_count
            source_total += src_count
            if inserted > 0:
                self.shard_last_insert[slug] = time.time()
        # Completeness snapshot for the health surface.
        self.completeness_index_total = index_total
        self.completeness_source_total = source_total
        if self.idx_con is not None:
            await asyncio.to_thread(
                update_hostname_state, self.idx_con, HOSTNAME, len(dbs), None
            )
            await asyncio.to_thread(self.idx_con.commit)
        self.reconciles_total += 1
        self.last_reconcile_ts = time.time()
        if total_inserted > 0:
            self.events_inserted_total += total_inserted
            self.last_insert_ts = time.time()
        dur = time.time() - t0
        logging.info(
            "reconcile #%d: %d shards, +%d recovered, %d/%d indexed, %.2fs",
            self.reconciles_total, len(dbs), total_inserted,
            index_total, source_total, dur,
        )
```

Add this module-level helper near the top of `scripts/v2/rollup_daemon.py` (after the imports, ~line 76):

```python
def _shard_event_count(db_path: Path) -> int:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    try:
        return con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        con.close()
```

And initialize the two counters in `RollupDaemon.__init__` (after `self.last_insert_ts = 0.0`, ~line 152):

```python
        self.completeness_index_total = 0
        self.completeness_source_total = 0
```

- [ ] **Step 4: Smoke-test the daemon import + one reconcile pass**

Run:
```bash
uv run --extra streaming python -c "
import asyncio, sys; sys.path.insert(0,'scripts/v2')
from rollup_daemon import RollupDaemon
d = RollupDaemon()
d._open_index()
asyncio.run(d._run_reconcile())
print('reconcile ok:', d.completeness_index_total, '/', d.completeness_source_total)
"
```
Expected: prints `reconcile ok: <index_total> / <source_total>` with no traceback. (This runs against the LIVE index — it recovers real missing events; that is intended and idempotent.)

- [ ] **Step 5: Commit**

```bash
git add scripts/v2/rollup_daemon.py
git commit -m "feat: daemon safety-net uses reconcile_project (true completeness) (task-4134)"
```

---

### Task 4a: Write completeness into daemon-health.json

**Files:**
- Modify: `scripts/v2/rollup_daemon.py` (`_write_health`)

- [ ] **Step 1: Add the completeness block to the health payload**

In `scripts/v2/rollup_daemon.py`, in `_write_health` (the `payload = {...}` dict, ~line 336), add after `"tracked_shards": len(self.shard_max_ts),`:

```python
            "index_completeness": {
                "index_total": self.completeness_index_total,
                "source_total": self.completeness_source_total,
                "missing": max(0, self.completeness_source_total - self.completeness_index_total),
                "pct": round(
                    100.0 * self.completeness_index_total / self.completeness_source_total, 3
                ) if self.completeness_source_total else 100.0,
            },
```

- [ ] **Step 2: Verify the JSON renders**

Run:
```bash
uv run --extra streaming python -c "
import asyncio, json, sys; sys.path.insert(0,'scripts/v2')
from rollup_daemon import RollupDaemon, HEALTH_PATH
d = RollupDaemon(); d._open_index()
asyncio.run(d._run_reconcile()); d._write_health()
print(json.dumps(json.loads(HEALTH_PATH.read_text())['index_completeness'], indent=2))
"
```
Expected: prints an `index_completeness` object; `pct` should be ~100.0 after the reconcile recovered the missing events.

- [ ] **Step 3: Commit**

```bash
git add scripts/v2/rollup_daemon.py
git commit -m "feat: surface index_completeness in daemon-health.json (task-4134)"
```

---

### Task 4b: Accessor surfaces completeness in stats() + healthz()

**Files:**
- Modify: `web/logging_accessor.py` (`stats`, `healthz`)
- Test: `tests/test_rollup_reconcile.py` (add)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rollup_reconcile.py`:

```python
def test_accessor_reads_completeness(tmp_path, monkeypatch):
    import json
    sys.path.insert(0, str(PLUGIN_ROOT / "web"))
    import logging_accessor

    root = tmp_path / "logging"
    (root / "_index").mkdir(parents=True)
    _make_index(root / "_index" / "index.db").close()
    (root / "_index" / "daemon-health.json").write_text(json.dumps({
        "index_completeness": {"index_total": 95, "source_total": 100, "missing": 5, "pct": 95.0}
    }))

    acc = logging_accessor.LoggingAccessor(root=root)
    comp = acc.stats().get("completeness")
    assert comp is not None and comp["missing"] == 5 and comp["pct"] == 95.0
```

Note: confirm the `LoggingAccessor` constructor accepts `root=` and exposes `self.index_db` / `self.root`. If the constructor differs, read the top of `web/logging_accessor.py` and adapt the instantiation in this test accordingly (the production code paths in Steps 3 already use `self.index_db` and `self.root`).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/test_rollup_reconcile.py::test_accessor_reads_completeness -v`
Expected: FAIL — `completeness` is `None`.

- [ ] **Step 3: Add a completeness reader + wire into stats() and healthz()**

In `web/logging_accessor.py`, add a private helper method to the `LoggingAccessor` class (place it just before `def stats` at ~line 116):

```python
    def _completeness(self) -> dict[str, Any] | None:
        """Read the daemon's last index-completeness snapshot, if present."""
        health = self.index_db.parent / "daemon-health.json"
        if not health.exists():
            return None
        try:
            import json
            return json.loads(health.read_text()).get("index_completeness")
        except Exception:
            return None
```

In `stats()`, before the `return {` of the success branch (~line 143), build and include it:

```python
            completeness = self._completeness()
```

and add to the returned dict (after `"last_synced_at": last_synced,`):

```python
                "completeness": completeness,
```

In `healthz()`, after the index query block (after ~line 233, before the heartbeat check), add:

```python
        completeness = self._completeness()
        if completeness and completeness.get("missing", 0) > 0:
            issues.append(
                f"index incomplete: {completeness['missing']} events missing "
                f"({completeness.get('pct', 0)}%)"
            )
```

and include `"completeness": completeness,` in the dict that `healthz()` returns.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_rollup_reconcile.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Restart the webui service (Python change) + commit**

```bash
systemctl --user restart claude-webui-platform
git add web/logging_accessor.py tests/test_rollup_reconcile.py
git commit -m "feat: accessor surfaces index completeness in stats/healthz (task-4134)"
```

---

### Task 5: Live recovery + verification (the payoff)

**Files:** none (operational)

- [ ] **Step 1: Dry-run the reconcile against live data**

Run: `uv run python scripts/v2/rollup_index.py --reconcile --dry-run`
Expected: prints per-shard DRIFT lines and a total ≈ 628 (the corpus-wide missing UserPromptSubmit + other types; total events may exceed 628 since the gap spans all types).

- [ ] **Step 2: Capture before-counts**

Run:
```bash
sqlite3 ~/.claude/local/logging/_index/index.db \
  "SELECT COUNT(*) AS idx_prompts FROM events_index WHERE type='UserPromptSubmit';"
```
Record the number (expected ~11,902).

- [ ] **Step 3: Run the live reconcile**

Run: `uv run python scripts/v2/rollup_index.py --reconcile`
Expected: `=== RECONCILE: recovered N events across M shards ===` with N > 0.

- [ ] **Step 4: Verify count parity**

Run:
```bash
sqlite3 ~/.claude/local/logging/_index/index.db \
  "SELECT COUNT(*) FROM events_index WHERE type='UserPromptSubmit';"
```
Expected: ~12,530 (matches per-project source sum; small delta only from in-flight writes).

- [ ] **Step 5: Verify the original symptom is fixed (session 9a19ae8e title)**

Run:
```bash
sqlite3 ~/.claude/local/logging/_index/index.db \
  "SELECT ts, substr(content_preview,1,60) FROM events_index
   WHERE session_id='9a19ae8e' AND type='UserPromptSubmit'
   ORDER BY ts ASC LIMIT 3;"
```
Expected: the FIRST row is now the 07:45 "I want you to create the claude-relationships plugin" prompt (previously absent).

- [ ] **Step 6: Restart the daemon so the new safety-net code is live**

Run: `systemctl --user restart claude-logging-rollup-daemon`
Then confirm health shows completeness:
```bash
sleep 8 && python -c "import json,pathlib;print(json.loads((pathlib.Path.home()/'.claude/local/logging/_index/daemon-health.json').read_text()).get('index_completeness'))"
```
Expected: `pct` at/near 100.0.

- [ ] **Step 7: Final commit (close-out note)**

```bash
git commit --allow-empty -m "chore: rollup completeness recovered + daemon hardened live (task-4134)"
```

---

## Self-Review

**Spec coverage:**
- `reconcile_project` anti-join + fast path + projection + event_count → Task 1 ✓
- One-shot CLI `--reconcile` / `--dry-run` → Task 2 ✓
- Daemon `_run_reconcile` uses reconcile → Task 3 ✓
- Health completeness (daemon-health.json + accessor stats/healthz) → Task 4a/4b ✓
- Regression test (sub-watermark event) → Task 1 `test_reconcile_recovers_sub_watermark_event` ✓
- Idempotency / fast-path / FTS parity tests → Task 1 ✓
- Live count parity + title fix + truthful event_count → Task 5 ✓

**Placeholder scan:** none — every code/test step shows full content; the one judgment call (LoggingAccessor constructor signature) is flagged with explicit fallback instructions in Task 4b Step 1.

**Type consistency:** `reconcile_project` returns `(inserted, index_count)` consistently across Task 1 (definition), Task 2 (`run_reconcile_all` unpacks `recovered, _`), Task 3 (`_reconcile_one` unpacks `inserted, idx_count`). `_shard_event_count`, `_index_count`, `_upsert_event_count` names used consistently. Health key `index_completeness` (daemon) → accessor reads `index_completeness`, exposes as `completeness` — consistent within each layer.

**Out of scope held:** no rowid rewrite, no hot-path watermark change, no multi-machine merge, no webui render edits.

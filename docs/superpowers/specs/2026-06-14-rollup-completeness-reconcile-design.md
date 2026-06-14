# Rollup completeness — true reconcile pass

**Date:** 2026-06-14
**Author:** Matt
**Backlog:** task-4134
**Branch:** `fix/rollup-completeness-reconcile`
**Status:** approved design → implementation

## Problem

The cross-project rollup (`_index/index.db`) is missing events that exist in the
per-project source DBs. Measured 2026-06-14:

- UserPromptSubmit: per-project sum **12,530** vs index **11,902** → **628 missing (~5%)**.
- Project `-home-shawn-.claude-plugins-local-legion-plugins`: 2,551 source vs 2,521 index → 30 missing.

User-visible symptom: session `9a19ae8e`'s true first prompt
("I want you to create the claude-relationships plugin", 07:45:03 UTC) is absent
from the index, so the webui card is titled with the *second* prompt and the
"started" vs first-prompt timestamps disagree. Counts, titles, FTS search, and
prompt lists all silently under-report.

## Root cause (confirmed in code)

`rollup_index.py::rollup_project()` ingests `WHERE ts > last_event_ts` (a per-project
ts watermark in `rollup_state`). Claude Code sessions change cwd mid-run, so one
session's events split across multiple per-project DBs, and within a DB events can
be written **out of global ts order** (a late event with an *earlier* ts than the
current watermark). Once the watermark passes that ts, `ts > watermark` skips the
event **forever**.

The daemon (`rollup_daemon.py`) has a 5-minute `reconcile_loop` →
`_run_reconcile()` that calls `_rollup_one()` for every shard — but `_rollup_one`
calls the **same** watermark-bound `rollup_project()`. So the advertised
"safety net" structurally **cannot** catch sub-watermark events. The reconcile is
a watermark replay, not a completeness check.

`rollup_state.event_count` is written as the *insert delta* (often 0), not the true
count, so the gap is invisible to health checks.

## North star

`_index/index.db` is a provably complete projection of the per-project DBs:
every source `event_id` is present; `COUNT(*)` per project matches source (within
in-flight write tolerance). The rollup is idempotent and gap-free under
out-of-order writes.

## Design principles

- **`event_id` is the unit of truth, not ts.** Dedup/ingest by `event_id`
  (already the `events_index` PRIMARY KEY).
- **Reconcile, don't trust.** Periodic source-vs-index COUNT surfaces drift.
- **Idempotent.** `INSERT OR IGNORE` by `event_id`; safe to re-run (re-run = 0).
- **No source mutation.** Read per-project, write index only.
- **DRY.** The one-shot recovery and the permanent daemon guard are the *same*
  function.

## Architecture

One new function, two call sites. Everything else reused.

### 1. `reconcile_project(idx_con, slug, db_path) -> (inserted, index_count)` in `rollup_index.py`

A true completeness pass for one shard:

1. **Fast path** — compare counts:
   - source: `SELECT COUNT(*) FROM events` (via read-only `ATTACH` or `proj_con`)
   - index: `SELECT COUNT(*) FROM events_index WHERE project_slug = ?`
   - Equal → update `rollup_state.event_count` to the index count, return `(0, n)`.
     (The microsecond common case — no drift.)
2. **On mismatch** — anti-join recover:
   - `ATTACH 'file:<db_path>?mode=ro' AS src`.
   - Pull missing rows:
     ```sql
     SELECT e.id, e.session_id, e.type, e.ts, e.persona, e.content
     FROM src.events e
     LEFT JOIN events_index x ON x.event_id = e.id
     WHERE x.event_id IS NULL
     ```
   - Project each row with the **same** projection as `rollup_project`
     (`content_preview = content[:200]`, `has_full_content = len(content) > 200`).
   - `INSERT OR IGNORE INTO events_index (...)` + `INSERT INTO events_index_fts (...)`
     for exactly the recovered `event_id`s, in one transaction.
   - `DETACH src`.
   - Update `rollup_state.event_count` to the new true index count.
   - Return `(inserted, index_count)`.

`event_id` is globally unique (`evt_<hash>`) and the index PK, so the anti-join
joins on `event_id` alone (no project scoping needed). FTS rows are inserted only
for recovered events (FTS5 has no UNIQUE constraint, so we insert the exact delta
rather than `INSERT OR IGNORE`).

### 2. Two call sites

- **CLI one-shot** — `rollup_index.py --reconcile`: run `reconcile_project` over all
  shards from `discover_dbs()`, print a summary (shards scanned, events recovered,
  per-shard drift). Recovers the 628 now. Dry-run via `--reconcile --dry-run`
  (count + report, no writes).
- **Daemon** — `_run_reconcile()` swaps its per-shard `_rollup_one` call for
  `reconcile_project`. The existing 5-min `reconcile_loop` becomes a real
  completeness pass. The hot-path watermark (`debounce_flush_loop` → `_rollup_one`
  → `rollup_project`) is **untouched** — it keeps low-latency incremental ingest;
  correctness is guaranteed by the reconcile.

### 3. Health surface

- `daemon-health.json`: add `index_completeness` block — `index_total`,
  `source_total` (summed over shards during reconcile), `missing`, `pct`.
- Accessor `healthz()` / Stats tab: surface completeness % so future drift is
  visible (332 Watch/Surface).

## Data flow

```
hot path (latency):   WAL write → inotify → debounce → rollup_project (ts>wm) → index
safety net (truth):   every 5min → reconcile_project per shard:
                        COUNT match? → done
                        mismatch?   → ATTACH → anti-join → INSERT OR IGNORE → DETACH
one-shot recovery:    rollup_index.py --reconcile  (same reconcile_project)
```

## Error handling

- Reconcile reuses the daemon's per-shard `try/except` degraded-shard guard:
  a shard that errors (schema drift, locked DB) is logged once and skipped, not
  fatal to the pass.
- `ATTACH` failures (missing/corrupt source) → caught per shard, shard marked
  degraded.
- Read-only `ATTACH` (`mode=ro`) guarantees no source mutation.
- WAL on the index connection + `INSERT OR IGNORE` make the one-shot CLI safe to
  run concurrently with the live daemon.

## Testing

- **Regression (the exact bug):** insert a synthetic event with `ts < watermark`
  into a temp project DB; assert it is absent after `rollup_project` but present
  after `reconcile_project`.
- **Idempotency:** run `reconcile_project` twice; second run inserts 0.
- **Fast path:** matching counts → 0 inserts, no `ATTACH`.
- **FTS parity:** recovered events are searchable via `events_index_fts`.
- **Count parity (live):** after one-shot `--reconcile`, corpus
  `UserPromptSubmit` index count == per-project sum (628 → 0).
- **Title fix:** session `9a19ae8e` card title becomes its true first prompt
  (07:45 claude-relationships).

## Acceptance criteria

- [ ] `index` UserPromptSubmit count == per-project sum (± in-flight writes).
- [ ] Session `9a19ae8e` card title = true first prompt.
- [ ] `reconcile_project` idempotent (re-run = 0 inserts).
- [ ] Synthetic sub-watermark event appears in index after reconcile.
- [ ] daemon-health.json + Stats report `index_completeness`.
- [ ] `rollup_state.event_count` holds the true per-project index count.

## Risks / mitigations

| Risk | Mitigation |
|---|---|
| Concurrent daemon vs one-shot CLI | index WAL + `INSERT OR IGNORE` idempotency; both safe. |
| FTS row divergence | recovered FTS rows inserted in same txn as `events_index`. |
| `ATTACH` cost every reconcile | gated behind COUNT-compare fast path; `ATTACH` only on drift. |
| Large drift (first run, 628) | one pass; bounded by source size; runs in seconds. |
| Source DB locked mid-reconcile | read-only `ATTACH` + per-shard degraded guard; retried next pass. |

## Out of scope

- rowid-cursor rewrite of the hot path (rejected: VACUUM/compaction-fragile).
- Multi-machine index merge (hostnames table already present; untouched).
- Webui card-render changes (the title fix falls out of the data fix).

## Open questions

- None blocking. Cursor strategy resolved (reconcile sweep, not rowid).

# claude-logging Reliability: Continuous Capture with Provable Completeness

Date: 2026-07-15
Status: Design, approved for planning
Author: Claude (with Shawn)

## 1. Problem

The log store captured nothing between 2026-06-30T23:00:27Z and 2026-07-15. Fifteen days, zero events, no alarm.

The reported symptom was "lag." The reality was total silence. The store held 16,990 events across 15 sessions and had been frozen for two weeks.

### Root cause (confirmed by experiment, not inference)

The plugin manifest lived at `plugin.json` (repo root). Claude Code reads the manifest only from `.claude-plugin/plugin.json`. It never found one, so the plugin's 25 hook declarations were never registered.

This was fixed by adding `.claude-plugin/plugin.json`, and the fix is proven: a headless session captured `SessionStart`, `UserPromptSubmit`, `Stop`, `SessionEnd`, each exactly once, taking the store from 15 to 16 sessions.

### Why it hid for fifteen days

Skills, commands, and agents load by directory convention (`skills/`, `commands/`, `agents/` are auto-discovered without a manifest). Hooks are the only component that requires the manifest. So the plugin presented as healthy: slash commands worked, the archivist agent worked, skills appeared in the picker. Only the silent half was dead.

A partial-load failure is more dangerous than a total one. Every signal a human would naturally check said "fine."

`claude plugin validate` reports the error in about one second. It had never been run.

### Two hypotheses that testing killed

Recorded because they are plausible and wrong, and the next person will reach for them:

1. "Hooks must live in `hooks/hooks.json`." False. Inline hooks in the manifest work. `claude-voice` does exactly this and fires correctly. Adding `hooks/hooks.json` changed nothing.
2. "Edit the plugin cache at `~/.claude/plugins/cache/...`." False. Claude Code loads this plugin from `~/Workspace/legion-plugins/plugins/claude-logging` via the `~/.claude/plugins/local/legion-plugins` symlink. Cache edits are inert.

## 2. Goal

**Provable completeness, not guaranteed uptime.**

We will not promise 100% reliability, and we should not pretend to. Capture depends on Claude Code choosing to invoke our hooks, a contract that already changed underneath us once without warning. Nothing we write prevents that.

What we can guarantee instead:

- The store is allowed to fall behind, but never *silently*.
- The store always converges back to complete.
- Completeness is a **checkable assertion**, not a hope.

### The completeness invariant

> For every session `S` present in `~/.claude/projects/<slug>/*.jsonl` (excluding subagent transcripts), there exists a session row in the store for `S`, and the store's event count for `S` is at least the floor derivable from `S`'s transcript.

Violations of this invariant are countable. A gap audit that reports zero violations is a proof of completeness. That is the closed loop the system has always lacked.

### Non-goals

- Strict ACID consistency across JSONL and SQLite. JSONL is the source of truth; SQLite is a derived index. Bounded-window, self-healing convergence is the correct trade.
- Recovering hook-only signals that transcripts never contained (see 6.3).
- Rebuilding anything that already exists (see 3).

## 3. Key insight: this is an integration problem, not a build problem

The reliability machinery is **already written and simply unwired**:

| Asset | State |
|---|---|
| `contrib/heartbeat_check.py` | Complete, 21 passing tests, thresholds, JSON output, alerting. Wired to no cron and no timer. |
| `scripts/v2/rollup_daemon.py` | Finished realtime daemon: `watchfiles` on WAL, 100ms debounce, 5-minute reconcile safety net, health loop writing `daemon-health.json`. Never deployed as a service. |
| `origin/fix/rollup-completeness-reconcile` | 14 unmerged commits implementing anti-join completeness reconcile, `--reconcile`/`--dry-run`, completeness surfaced in healthz. |
| `scripts/extract_session_text.py` | Already parses native transcripts. Tested. |
| Test suite | 271 pass. **5 already fail against live data, detecting this exact breakage.** Nobody runs them. |
| `claude-meetings/scripts/migrate_003_fts_external_content.py` | Fleet precedent for the FTS5 fix on a `TEXT PRIMARY KEY` table. |

The build bias is the trap here. Almost every phase below is wiring, porting, or merging.

## 4. Evidence base

Every number measured on this machine, on the real 88MB store.

| Measurement | Value | Implication |
|---|---|---|
| Inline sync cost (real 88MB DB, populated FTS) | 0.020ms median, 0.082ms p95 | Inline sync is ~0.05% of a hook. Turn-boundary batching buys nothing. |
| Hook invocation, empty session | 35ms | `uv` spawn floor is ~19ms of it. |
| Hook invocation, 14.7MB session | 83ms (2.4x) | `get_agent_session_num()` re-parses the whole JSONL per event. O(n) per event. |
| FTS5 `INSERT OR REPLACE`, same id twice | **2 rows** | Proven broken. Re-sync duplicates silently. |
| Transcripts covering the gap | 1,538 files | Backfill is viable. |
| Retention | Was default 30d, **now 365d** | Deadline neutralized (Jun 30 data would have expired ~Jul 30). |
| Verification harness | `claude -p` fires all hooks | Objective pass/fail gate, no session restart needed. |

## 5. Architecture

Five layers. Each is independently checkable, and no single failure is fatal.

```
Layer 0  Correct by construction   manifest + validate gate + registration test
Layer 1  Realtime capture          hooks -> JSONL (truth) -> inline SQLite sync
Layer 2  Independent floor         transcript reconciler (survives total hook death)
Layer 3  Detection                 heartbeat watchdog (minutes, not weeks)
Layer 4  Convergence proof         gap audit against the completeness invariant
```

### The load-bearing design decision

Layer 1 is **best-effort**. Layer 2 is **guaranteed**.

Inline sync may fail (SQLITE_BUSY, crash, bug). That is acceptable, because JSONL is the source of truth and the reconciler repairs the difference. Neither layer is a single point of failure.

Critically, Layer 2 reads from `~/.claude/projects/` and **not** from the plugin's own JSONL. This breaks a circular dependency that would otherwise defeat the whole design: hooks are currently the only capture path, so when hooks die, no JSONL is written and there is nothing to recover from. A reconciler tailing our own JSONL would inherit that exact single point of failure. One tailing Claude Code's native transcripts does not. That is why the fifteen-day hole is recoverable at all.

### Why not a single-writer daemon

The audit raised SQLITE_BUSY contention under many concurrent hook processes and floated a single-writer daemon. Rejected as premature: measured transaction cost is 0.02ms, so even dozens of concurrent processes leave the write lock idle >99% of the time. Phase 2's single-transaction batching (4+ commits down to 1) is the real lever and is far cheaper than an always-on daemon, which would itself reintroduce the silent-death failure mode we are eliminating. Revisit only if the Phase 3 concurrency test shows measurable BUSY rates.

## 6. Phases

Ordering is dependency-driven, not preference. **Phase 2 must precede Phase 4**: backfill re-syncs events by design, and re-syncing on today's FTS5 bug silently duplicates every repaired row. Shipping the repair before the bug fix would corrupt the index it is meant to heal.

### Phase 0: Restore capture (DONE, PROVEN)

- [x] `.claude-plugin/plugin.json` added. Validation passes. Hooks fire. Store 15 -> 16.
- [x] `cleanupPeriodDays: 365` (settings.json backed up first).

### Phase 1: Lock in the fix

Goal: this class of failure becomes structurally impossible to ship again.

1. Resolve the duplicate manifest. Root `plugin.json` is now vestigial and ignored. Make `.claude-plugin/plugin.json` canonical; remove or reduce the root file deliberately after checking whether other legion tooling reads it.
2. Resolve the name mismatch: manifest declares `"name": "logging"` while the directory and marketplace entry say `claude-logging`. Debug output confirms hooks register under `logging`. Pick one and align.
3. **Registration test** (the test that would have caught this on day one): assert `claude plugin validate` passes, and assert that a `claude -p` session produces a new session row. This is the only test that verifies the plugin's actual contract with its host.
4. CI gate: run `claude plugin validate` on every plugin in the repo.
5. Schedule the existing live-data tests. They already detect this failure; they just need to run.

**Exit:** a deliberately broken manifest fails CI.

### Phase 2: Sync correctness (BLOCKS Phase 4)

1. **FTS5 external-content migration.** Port `claude-meetings/scripts/migrate_003_fts_external_content.py`. Use `content=events, content_rowid=rowid` (SQLite's hidden rowid, not the TEXT `id`), with AFTER INSERT/DELETE/UPDATE triggers guarded on `content IS NOT NULL AND content != ''` to match current `if event.content:` behavior. Delete the manual `events_fts` write from `insert_event()`; triggers own it. Backup the 88MB DB first, then `'rebuild'`. This makes duplication *structurally* impossible rather than closing one code path, and it is the single change that makes backfill idempotent.
2. **Torn-line cursor.** Track `good_through` as the offset after the last successfully parsed, newline-terminated line. `break` on decode failure, never `continue`. Drop the pre-read `current_pos` snapshot entirely.
3. **Single transaction per `sync_session()`.** Wrap N inserts plus `update_sync_position` plus `insert_session` in one `BEGIN IMMEDIATE`. Cuts write-lock acquisitions from `4+N` to `1` and fixes the partial-crash window.
4. Fix `busy_timeout`: `timeout=10` at connect is silently overridden by `PRAGMA busy_timeout=5000`. Make it one honest value.
5. Guard `_update_session_from_events` cwd: only trust `cwd` from an actual `SessionStart` event, not from whatever line happened to be first in the sync window.

**Exit:** re-running a full sync over already-synced data produces zero duplicate FTS rows and zero dropped events. Regression test for both.

### Phase 3: Realtime inline sync

1. Sync inline on every event rather than only at turn boundaries. Measured cost: ~0.05% of the hook.
2. Keep failures non-fatal and logged. Layer 2 repairs.
3. Concurrency test: N concurrent processes, assert SQLITE_BUSY rate near zero. If not, revisit the daemon decision.
4. Address the O(n) hot path: `get_agent_session_num()` re-parses the entire session JSONL on every event (35ms -> 83ms at 14.7MB). Cache it or track incrementally.

**Exit:** DB is current within milliseconds of each event; lag is unmeasurable.

### Phase 4: Transcript reconciler and backfill

1. **Schema (additive, per the AGENTS.md invariant):** add `source TEXT` to `events`, values `hook` or `transcript`. Provenance must be auditable; a transcript-derived event is not the same artifact as a hook-captured one, and the difference must never be silently erased.
2. **Mapper** from native transcripts to `events` rows. Per the schema research: `PreToolUse`, `Stop`, `SessionStart`, `UserPromptSubmit`, `SubagentStart`, `PostToolUseFailure` are recoverable from `type:"attachment"` where `attachment.type=="hook_success"`. `PostToolUse` derives from the `tool_use`/`tool_result` pair. Compaction derives from `system/compact_boundary`. Timestamps are ISO8601 UTC but **not strictly monotonic within a file**; sort, do not assume file order.
3. **Session-level gate, not event-level merge.** A session is either hook-covered or transcript-derived. Never mix. This avoids double-counting entirely and keeps dedup logic out of the hot path. Mixed-coverage sessions get flagged for review rather than silently reconciled.
4. **Gap audit** implementing the completeness invariant: `transcript_sessions - store_sessions`. Reports violations. Converges to zero.
5. Backfill the Jun 30 to Jul 15 hole. **Snapshot the 88MB DB first.**
6. Review and likely merge `origin/fix/rollup-completeness-reconcile` before writing any of this. It already implements the anti-join reconciler.

**Exit:** gap audit reports zero missing sessions for the outage window; all backfilled rows carry `source='transcript'`.

### Phase 5: Detection

1. Create `~/.claude/local/health/heartbeats.yml` (`heartbeat_check.py` currently falls back to defaults because it is missing).
2. systemd user timer running the reconciler plus heartbeat check every 5 minutes. Repairs and reports in one pass.
3. Alert on stale heartbeat or non-empty gap audit. Route through the existing Telegram path.
4. Evaluate deploying `scripts/v2/rollup_daemon.py` as a service, and re-init the cross-project `_index/index.db` (it does not exist on this machine; the historical 395K/205-project figure was a different host).
5. `/logging:doctor`: validate, registration, heartbeat age, gap audit, in one command.

**Exit:** kill capture deliberately, and the system reports it within 5 minutes.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Backfill corrupts the live 88MB index | Phase 2 precedes Phase 4, by construction. Snapshot before write. Idempotency regression test. |
| Hooks cannot hot-swap; changes need a new session | `claude -p` harness verifies without restarting the user's session. |
| Claude Code changes the plugin contract again | Exactly why Layer 2 does not depend on hooks, and Layer 3 detects in minutes. The validate gate catches manifest-shaped breakage at CI time. |
| Transcript format is a moving target | Pin the mapper to observed fields; version it; the mapper is a floor, not the primary path. |
| Fixing FTS on a live DB | Ported from proven fleet precedent; backup first; verify row counts after `'rebuild'`. |

## 8. Open items

- The historical cause remains unexplained: the store holds data through 2026-06-30T23:00Z, roughly 30 minutes after `installedAt: 2026-06-30T22:30Z`, yet no `log_event.py` execution appears in any retained transcript (retention starts Jun 24). Either pre-Jun-30 capture ran through a registration path since removed (settings.json hooks are currently empty), or silent hooks left no trace. **Not on the critical path** (the fix is proven regardless), but worth resolving because it may indicate a second latent failure mode.
- Neighbouring plugins are broken the same way: `claude-finance` has an invalid manifest and fails to load entirely; `claude-meetings` has a skills path pointing at a file instead of a directory. The Phase 1 CI gate should cover the whole repo.
- `claude-messages` and `claude-dock` log `hook-load-failed` from declaring `"hooks": "./hooks/hooks.json"` in the manifest when the standard file already auto-loads. Harmless today but noisy and wrong; fix while we are here.

# Runtime adapters

The archive and read models are runtime-neutral. Host-specific code belongs at
the capture edge and annotates native hook payloads before passing them to
`hooks/log_event.py::process_event`.

Every stored event carries:

- `runtime`: stable harness identifier (`claude`, `codex`, later `pi`, `hermes`)
- `runtime_event`: the native hook event name
- `turn_id`: the host's turn identifier when available
- `capture_source`: hook, archive backfill, or another ingest edge
- `model` and `permission_mode`: optional native execution context
- `source_kind`: `live`, `archive`, `backfill`, or `synthetic`

The stable minimum is implemented by `lib/event_contract.py` as
`legion.logging.event.v1`. Capture validates this envelope before any adapter
event is persisted. Canonical `type` and native `runtime_event` are deliberately
separate: consumers group common behavior by `type` while forensic views retain
the harness's original vocabulary.

Existing databases migrate additively on open. Rows created before this
contract are classified as `runtime=claude`. The cross-project rollup also
handles cold legacy shards that have not yet been opened by a current hook.

## Codex

Codex discovers `.codex-plugin/plugin.json` and `hooks/hooks.json`. The hook
commands invoke `adapters/codex/log_event.py`, which adds Codex provenance and
delegates to the shared capture core. Codex supplies `last_assistant_message`
on `Stop`; the adapter path stores that response directly and does not parse a
Claude transcript.

The WebUI runtime selector filters prompts, transcripts, and search results.
`/api/stats` and `/healthz` expose event counts by runtime.

## Adding another harness

Implement a thin adapter that maps lifecycle names, preserves the full native
payload, sets `_runtime` and `_capture_source`, and calls `process_event`.
Do not copy storage, rollup, search, health, or UI implementations. If a host
lacks a lifecycle hook, add a host-specific archive reader that emits the same
provenance fields and clearly identifies its capture source.

## Proven adapter contract (v1)

The three integrated sources now prove two complementary edges. A harness may
implement either or both:

### Capture edge

```text
normalize(native_event) -> legion.logging.event.v1
```

It must preserve the native payload and provide stable `runtime`,
`runtime_event`, `capture_source`, and `source_kind` values. Capture writes the
shared JSONL archive; storage, indexing, search, statistics, and UI remain
shared infrastructure.

### Archive/read edge

```text
discover_sessions(cursor, limit, filters)
get_session(source_session_id)
backfill(checkpoint, dry_run)
health()
capabilities()
```

Discovery is paginated and deterministic. Session/event identities must remain
stable across rebuilds. Backfills are idempotent and never replace sessions
already owned by live capture. `capabilities()` declares optional fidelity
(tools, reasoning, tokens, costs, artifacts, subagents, permissions, models)
so consumers do not infer support from missing fields.

The rollup reconciles unseen event IDs in addition to advancing its timestamp
high-water mark. This is essential: an archive import commonly introduces old
events after newer live events have already been indexed.

### Current capability evidence

| Source | Live | Backfill/archive | Tools | Reasoning | Tokens | Artifacts |
|---|---:|---:|---:|---:|---:|---:|
| Claude Code | yes | yes | yes | bounded | yes | yes |
| Codex | yes | yes | yes | yes | yes | native payload |
| Claude Web | no | yes | no | no | no | yes |
| Pi | extension | session tree | yes | yes | provider usage | yes |
| Prime Agent | extension | session tree | yes | yes | provider usage | yes |
| OMP | extension | session tree | yes | yes | provider usage | yes |
| Hermes | shell hooks | SQLite session store | yes | yes | yes | native payload |

This contract is intentionally smaller than a universal harness SDK. Pi should
be the next implementation test; only requirements demonstrated by that fourth
source should be added to v2.

## Pi proof

Pi demonstrates that `Session -> ordered Events` is insufficient as the
authoritative universal model. Its v2/v3 session entries form a tree through
`id` and `parentId`; forks and navigation can preserve multiple branches in one
session file. The normalized event envelope therefore retains `parent_id` in
native data. The current timeline is a chronological projection, while a
future session-tree view can reconstruct branches without re-importing.

The archive adapter reads `~/.pi/agent/sessions/**.jsonl`, supports session
versions 1–3, and maps messages, tool calls/results, thinking, compaction,
branch summaries, model changes, and visible extension messages. The live Pi
extension uses Pi's own lifecycle API rather than polling its session files.

The same archive projector and lifecycle core now serve Prime Agent and OMP
with distinct runtime and capture-source identities. Their roots are
`~/.prime/agent/sessions` and `~/.omp/agent/sessions`. Prompt capture uses the
shared `before_agent_start` event because it fires for both interactive and
print-mode turns; the lower-level `input` event is UI-specific in OMP.

## Hermes proof

Hermes uses a dual edge. Nine consented observer-only shell hooks capture
session, prompt, response, API usage, tool, and subagent lifecycle events in
real time. The read-only `~/.hermes/state.db` projector supplies historical
recovery and enriches sessions with reasoning, tokens, costs, tool-call IDs,
model metadata, and parent-session lineage. Hermes doctor payloads are marked
`source_kind=synthetic` so conformance checks cannot pollute normal feeds.

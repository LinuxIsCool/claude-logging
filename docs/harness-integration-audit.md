# Harness integration audit

Evidence collected from the locally installed runtimes on 2026-08-09. This is
an implementation guide, not a claim that every harness exposes identical
semantics.

| Harness | Installed evidence | Best live edge | Archive edge | Recommended adapter |
|---|---|---|---|---|
| Pi 0.84.1 | extension API and v3 JSONL tree | extension lifecycle events | `~/.pi/agent/sessions/**/*.jsonl` | implemented reference |
| Prime Agent 0.7.1 | Pi-derived `ExtensionAPI`, `--extension`, saved sessions | Pi-family extension wrapper | native session JSONL | share Pi-family core; retain `runtime=prime-agent` |
| OMP 17.2.12 | `--hook`/`--extension`, Pi event vocabulary, JSONL sessions | Pi-family extension wrapper | profile-aware native session JSONL | share Pi-family core; retain `runtime=omp` |
| Hermes 0.18.2 | consented shell hooks, SQLite sessions/messages, ACP | nine shell-hook observers, enabled | `~/.hermes/state.db` projector, imported | implemented dual-edge adapter |

## What genuinely generalizes

The reusable center is the canonical event contract, stable identity helpers,
the append-only capture sink, index reconciliation, health/capability reporting,
and conformance fixtures. Runtime-specific code should stop at normalization and
session discovery.

Pi, Prime Agent, and OMP form a useful adapter family. Their common live surface
includes session start/shutdown, input, assistant message completion, tool
start/end, compaction, model selection, and thinking-level selection. A shared
TypeScript core can register these handlers while a tiny wrapper supplies the
runtime identifier, capture-source identifier, package types, and config path.
They must remain distinct runtimes in storage even when their implementation is
shared.

Hermes should not be forced through the Pi abstraction. Its shell-hook payload
already exposes stable tool-call and turn IDs, duration, status/error details,
model/platform context, and subagent lineage. Its SQLite store is richer than a
flat transcript: sessions contain parent lineage, costs, tokens, model usage,
handoff state, and counters; messages contain reasoning, tool calls/results,
effects, compaction/activity state, and provider-native reasoning/message items.
The correct design is dual-edge capture: hooks for latency, SQLite for recovery
and enrichment.

## Conformance gates

Every new adapter must prove:

1. Stable session and event IDs across repeated import.
2. Prompt, response, reasoning, tool start/result/failure fidelity.
3. Tool correlation through a native call ID when the host supplies one.
4. No duplicate conversational events after live capture plus archive backfill.
5. Preservation of branching/parent and subagent lineage.
6. Crash-safe writes and bounded hook latency.
7. Honest capability and health reporting for unsupported fields.
8. A controlled live probe compared against the native source of truth.

## Execution order from this audit

1. Extract the proven Pi extension handlers into a package-neutral Pi-family
   core and retain the current Pi wrapper as the reference fixture.
2. Add Prime Agent and OMP wrappers, configure one at a time, and run the same
   controlled fidelity probe used for Pi.
3. Implement the Hermes SQLite projector first because it is read-only and
   gives the fullest recovery path.
4. Add Hermes shell hooks after projector identities are stable, then reconcile
   hook and archive events without duplication.
5. Promote shared fixtures into an adapter conformance command and expose each
   adapter's declared capabilities in Runtime Adapters health cards.

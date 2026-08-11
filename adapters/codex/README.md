# Codex adapter

Captures Codex lifecycle events into Legion's shared logging archive,
stamping `runtime=codex` / `capture_source=codex-hook`.

## Installing

`hooks.json` in this directory is the Codex hook manifest. Install it from
**Codex's** configuration — do not place it at `plugins/claude-logging/hooks/hooks.json`.

That path is not inert. Claude Code auto-loads `hooks/hooks.json` for every
plugin, so a manifest sitting there is executed by *Claude Code sessions*,
in addition to the plugin's own `.claude-plugin/plugin.json` hooks. Both
manifests fire, and every event is written twice: once correctly as
`runtime=claude`, once mislabelled `runtime=codex`.

That is exactly what happened between 674ffe9 (2026-08-10) and 2026-08-11 —
26,895 phantom `codex-hook` rows across 428 project databases, ~46% duplicate
rows in the prompts feed, and Claude sessions appearing in cross-harness views
as though Codex had produced them.

The two manifests are not interchangeable:

| manifest | loaded by | stamps |
|---|---|---|
| `.claude-plugin/plugin.json` | Claude Code | `runtime=claude`, `capture_source=claude-hook` |
| `adapters/codex/hooks.json` | Codex | `runtime=codex`, `capture_source=codex-hook` |

If you need to verify which harness is writing, group by `capture_source` —
a Claude session that has produced `codex-hook` rows is double-capturing.

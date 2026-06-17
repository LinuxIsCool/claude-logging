"""provenance — classify UserPromptSubmit content as human signal vs machine noise.

task-4155 (EPIC: Logging → Living Self-Model of Shawn). Only ~62% of captured
UserPromptSubmit events are the human actually typing; the rest are background-
agent callbacks, dispatched persona/rhythm seed prompts, inter-agent matrix
messages, and slash commands. This is the single discriminator that lets the
corpus be treated as a person-model dataset.

Canonical classifier — imported by the capture hook (tag-at-capture) AND the
one-off backfill script (so the two never drift).

Provenance values:
  human-typed    — genuine human input (the Shawn-only subcorpus)
  task-callback  — background-agent completion callbacks (<task-notification>)
  agent-seed     — dispatched persona/rhythm/scheduled seed prompts
  agent-matrix   — inter-agent matrix messages (<channel ...>)
  slash-command  — a slash command invocation
  empty          — no content
"""
from __future__ import annotations

import re

SLASH_RE = re.compile(r"^/[a-z][\w:-]*\b")

# Dispatched-seed signatures: prompts the rhythm/persona/scheduler machinery
# submits to spawned sessions (matched against normalized content).
SEED_PREFIXES = (
    "- You are",
    "You are the ",
    "You are an ",
    "You are a ",
    "You are Legion",
    "You have woken up for a scheduled",
    "Run the nightly",
)
SEED_REGEXES = (
    re.compile(r"^Run the .{0,40}review", re.IGNORECASE),
    re.compile(
        r"^You are .{0,60}(orchestrator|investigator|observer|analyst|consolidator|narrator)",
        re.IGNORECASE,
    ),
)


def _norm(content: str) -> str:
    """Strip a leading bare-dash dispatch marker ('-\\n' / '- ') used by the
    rhythm/persona runner (`claude -p -`), so seed prefixes match."""
    c = content.strip()
    if c.startswith("-") and (len(c) == 1 or c[1] in " \t\r\n"):
        c = c[1:].strip()
    return c


def classify(content: str | None) -> str:
    """Return the provenance label for a UserPromptSubmit content string."""
    if not content:
        return "empty"
    c = _norm(content)
    if "<task-notification>" in content:
        return "task-callback"
    if c.startswith("<channel"):
        return "agent-matrix"
    if c.startswith(SEED_PREFIXES) or any(r.match(c) for r in SEED_REGEXES):
        return "agent-seed"
    first = c.split(None, 1)[0] if c else ""
    if SLASH_RE.match(first) and "\n" not in c[:80]:
        return "slash-command"
    return "human-typed"


def is_human(content: str | None) -> bool:
    return classify(content) == "human-typed"

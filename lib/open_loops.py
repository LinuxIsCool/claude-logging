"""open_loops — surface stated-but-unexecuted intentions from the human corpus.

task-4154 (EPIC 4152). A "plan-shaped" human prompt (let's build X / we should /
I want to / create a plan / implement …) whose session shows NO execution
activity afterward (no Write/Edit/Bash/NotebookEdit) is a candidate OPEN LOOP —
something Shawn asked for that may never have happened.

v1 is deliberately a heuristic *candidate* surface for human review, not a
precise classifier. It gets sharper once topic-threading (task-4158) can confirm
whether the intention was picked up in a *later* session. Honest about that.

Depends on the `human_prompts` view (task-4155).
Emits markdown + JSON to ~/.claude/local/logging/ for the brief / /status to read.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB = Path.home() / ".claude/local/logging/-home-shawn/db/logging.db"
OUT_DIR = Path.home() / ".claude/local/logging"

PLAN_RE = re.compile(
    r"\b(let'?s (build|create|make|plan|consider|do|add|design|set ?up|write|start)"
    r"|we (should|need to|could|want to|ought to|have to)"
    r"|i want (to|you to|us to)|i'?d like (to|you)"
    r"|create a (plan|complete)|implement the|build (a|the|out)"
    r"|plan for|next step|i need (to|you to)|should (build|create|add|make|set ?up)"
    r"|remind me to|brainstorm)\b",
    re.I,
)

# Eval-harness / fixture pollution that leaked into human_prompts.
NOISE_RE = re.compile(r"(\bS0\d\t|\tshould\t|what'?s the weather|eli5\b)", re.I)

# Generic "just keep going" templates — these are continuation commands, not
# dropped intentions. Excluded.
GENERIC_RE = re.compile(
    r"^\s*(please\s+)?(create|make)\s+(a\s+|the\s+)?(complete|comprehensive|thorough)"
    r"[\w\s]*plan(\s+for\s+(moving\s+forward|phase\s*\d+))?\.?\s*$",
    re.I,
)

MIN_LEN = 40          # drop trivially short plan-prompts
MIN_AGE_DAYS = 3      # a plan from today isn't "dropped" yet
EXEC_TOOLS = ("Write", "Edit", "Bash", "NotebookEdit", "MultiEdit")

_STOP = set(
    "the a an and or but to of for in on with this that have has had will would "
    "should could let lets let's we i you it me my our your please create make "
    "build plan want need do now just like about into them they here there".split()
)


def _salient(text: str) -> set[str]:
    """Distinctive terms: task-IDs, CapWords, and long lowercase tokens."""
    toks: set[str] = set()
    toks.update(re.findall(r"\btask-\d+\b", text, re.I))
    toks.update(re.findall(r"\b[A-Z][a-zA-Z]{3,}\b", text))   # CapWords / names
    for w in re.findall(r"\b[a-z][a-z\-]{4,}\b", text.lower()):
        if w not in _STOP:
            toks.add(w)
    return {t.lower() for t in toks}


def _norm_key(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())[:60]


OPEN_MIN_AGE_DAYS = 7          # older than this to count as "dropped"
PLANNING_INTENTS = ("PLN", "BST", "DEC")


def find_open_loops_llm(db: Path = DB) -> list[dict] | None:
    """v2 — INTERSECT strong signals (task-4158). The LLM open_loop flag alone
    over-produces (any forward-looking ask), and literal entity-string
    threading under-filters. So a real open loop requires ALL of:
      • LLM open_loop=1  (it stated an intention needing follow-through)
      • planning-class intent (PLN/BST/DEC) OR a substantive command (>200 ch)
      • NO Write/Edit/Bash execution after it in the same session
      • aged ≥ OPEN_MIN_AGE_DAYS
      • not eval-noise / generic-continuation, deduped
    Returns None if enrichment absent (caller falls back to regex)."""
    con = sqlite3.connect(str(db))
    try:
        if not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prompt_enrichment'"
        ).fetchone():
            return None
        rows = con.execute(
            "SELECT e.event_id, e.session_id, e.ts, e.intent, length(h.content), "
            "       e.projects, e.ventures, e.topics, e.rationale, e.sentiment, h.content "
            "FROM prompt_enrichment e JOIN human_prompts h ON h.id=e.event_id "
            "WHERE e.open_loop=1 ORDER BY e.ts"
        ).fetchall()
        if not rows:
            return None
        # per-session latest execution ts (one scan, via tool_name column)
        ph = ",".join("?" for _ in EXEC_TOOLS)
        last_exec = dict(con.execute(
            f"SELECT session_id, MAX(ts) FROM events WHERE type='PostToolUse' "
            f"AND tool_name IN ({ph}) GROUP BY session_id", EXEC_TOOLS
        ).fetchall())
        now = datetime.now(timezone.utc)
        seen: set[str] = set()
        loops = []
        for eid, sid, ts, intent, clen, proj, vent, topics, why, sent, content in rows:
            content = content or ""
            if NOISE_RE.search(content) or GENERIC_RE.match(content):
                continue
            substantive = intent in PLANNING_INTENTS or (intent == "CMD" and (clen or 0) > 200)
            if not substantive:
                continue
            if sid in last_exec and last_exec[sid] > ts:   # executed in-session
                continue
            try:
                age = (now - datetime.fromisoformat(ts)).days
            except ValueError:
                age = None
            if age is not None and age < OPEN_MIN_AGE_DAYS:
                continue
            key = _norm_key(content)
            if key in seen:
                continue
            seen.add(key)
            ent = [x.strip() for x in f"{proj},{vent},{topics}".split(",") if x.strip()]
            loops.append({
                "event_id": eid, "session_id": sid, "ts": ts, "age_days": age,
                "intent": intent, "sentiment": sent, "entities": ent[:6], "why": why,
                "preview": re.sub(r"\s+", " ", content.strip())[:200],
            })
        loops.sort(key=lambda x: -(x["age_days"] or 0))
        return loops
    finally:
        con.close()


def find_open_loops(db: Path = DB) -> list[dict]:
    con = sqlite3.connect(str(db))
    rows = con.execute(
        "SELECT id, session_id, ts, content FROM human_prompts ORDER BY ts"
    ).fetchall()
    now = datetime.now(timezone.utc)
    # Precompute the latest execution timestamp per session in ONE scan
    # (vs a LIKE-scan per plan-prompt — 405× over 226K rows was ~40s).
    placeholders = ",".join("?" for _ in EXEC_TOOLS)
    last_exec: dict[str, str] = {}
    for sid, mx in con.execute(
        f"SELECT session_id, MAX(ts) FROM events WHERE type='PostToolUse' "
        f"AND tool_name IN ({placeholders}) GROUP BY session_id",
        EXEC_TOOLS,
    ).fetchall():
        last_exec[sid] = mx
    seen: set[str] = set()
    loops: list[dict] = []
    for _id, sid, ts, content in rows:
        if not content or len(content) < MIN_LEN:
            continue
        if NOISE_RE.search(content) or GENERIC_RE.match(content):
            continue
        if not PLAN_RE.search(content):
            continue
        # (1) no execution after this prompt within the same session
        if sid in last_exec and last_exec[sid] > ts:
            continue
        try:
            age = (now - datetime.fromisoformat(ts)).days
        except ValueError:
            age = None
        if age is not None and age < MIN_AGE_DAYS:
            continue
        # NOTE: precise "was this intention picked up in a LATER session?"
        # needs entity-fingerprint threading (task-4158). A cheap shared-token
        # proxy was tried and rejected — common terms (venture/plugin/claude)
        # collide everywhere and zero out the list. Until 4158 lands, v1 is the
        # in-session-execution heuristic only, and is an explicit candidate
        # surface for review, not a precise classifier.
        sal = _salient(content)
        key = _norm_key(content)
        if key in seen:
            continue
        seen.add(key)
        loops.append(
            {
                "event_id": _id,
                "session_id": sid,
                "ts": ts,
                "age_days": age,
                "salient": sorted(sal)[:8],
                "preview": re.sub(r"\s+", " ", content.strip())[:200],
            }
        )
    con.close()
    loops.sort(key=lambda x: -(x["age_days"] or 0))
    return loops


def render_markdown(loops: list[dict], tier: str = "") -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    lines = [
        "# Open Loops — substantive intentions, likely dropped",
        f"_generated {today} · task-4154+4158 · {tier} · review-not-gospel_",
        "",
        f"**{len(loops)} candidates** — LLM open_loop=1 ∩ planning intent (PLN/BST/DEC "
        f"or big CMD) ∩ no in-session execution ∩ age ≥ {OPEN_MIN_AGE_DAYS}d:",
        "",
    ]
    for L in loops:
        age = f"{L['age_days']}d" if L["age_days"] is not None else "?"
        meta = []
        if L.get("intent"):
            meta.append(L["intent"])
        if L.get("sentiment") and L["sentiment"] != "neutral":
            meta.append(L["sentiment"])
        tag = f" _{'/'.join(meta)}_" if meta else ""
        lines.append(f"- **{age}**{tag} — {L['preview']}")
    if not loops:
        lines.append("_none — every substantive intention shows follow-through_")
    return "\n".join(lines) + "\n"


def main() -> int:
    loops = find_open_loops_llm()
    tier = "llm-enrichment intersection (task-4158)"
    if loops is None:
        loops = find_open_loops()
        tier = "regex heuristic (no enrichment yet)"
    print(f"[open-loops via {tier}]")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "open-loops.md").write_text(render_markdown(loops, tier), encoding="utf-8")
    (OUT_DIR / "open-loops.json").write_text(
        json.dumps(loops, indent=2), encoding="utf-8"
    )
    print(f"{len(loops)} open loops → {OUT_DIR}/open-loops.md")
    for L in loops[:15]:
        age = f"{L['age_days']}d" if L["age_days"] is not None else "?"
        print(f"  {age:>4}  {L['preview'][:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

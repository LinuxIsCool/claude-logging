"""Render the reverse-chronological prompt feed from logging.db.

Newest at top: roadmap above, live state in the middle, history receding as you
scroll. The file is generated, never hand-maintained, so it can always be
rebuilt from the transcripts rather than drifting away from them.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from lib.token_meter import open_db

# Set CLAUDE_PROMPT_LOG to render somewhere else. The default lives under the
# plugin's own data root; symlink it if you want the feed inside a notes repo.
DEFAULT_FEED = os.environ.get(
    "CLAUDE_PROMPT_LOG", "~/.claude/local/logging/prompt-log.md"
)

_HEADER = """# Prompt Log

Generated from logging.db for `{slug}`. Reverse chronological: newest at top.

Entry format: `## YYYY-MM-DD HH:MM TZ · <session>` where `<session>` is the first 8 chars
of the Claude Code session UUID, then an italic metadata line, then the prompt verbatim.
The session tag lets one log absorb parallel instances without losing which thread a
prompt belongs to. ITE = input-token-equivalents (cache reads 0.1x, writes 1.25x,
output 5x), and subagent spend is reported separately from the conversation's own.

GENERATED FILE. Do not hand-edit; edits are lost on the next render. Rebuild with
`python3 scripts/prompt_log.py render`. Slash-command expansions and hook injections
are recorded in the `prompts` table but excluded here.
"""


def _drop_resubmits(rows, window_seconds: int = 15):
    """Collapse the same text submitted twice in a session seconds apart.

    That is a rewind or a re-submit, not two thoughts. Deliberate repetition
    ("Please proceed." an hour later) falls outside the window and survives,
    which is why this is time-bounded rather than a plain DISTINCT on text.
    """
    kept, seen = [], {}
    for r in rows:
        sid, ts, text = r[1], r[2], r[4]
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            kept.append(r)
            continue
        key = (sid, text)
        prev = seen.get(key)
        if prev is not None and abs((prev - t).total_seconds()) <= window_seconds:
            continue
        seen[key] = t
        kept.append(r)
    return kept


def render_feed(slug: str, out: str = DEFAULT_FEED, limit: int = 500,
                only_if_exists: bool = False) -> int:
    """Write the feed. Returns prompts written, or -1 if skipped."""
    dest = Path(os.path.expanduser(out))
    if only_if_exists and not dest.is_file():
        return -1

    logging_root = Path.home() / ".claude" / "local" / "logging" / slug
    if not (logging_root / "db" / "logging.db").is_file():
        return -1

    conn = open_db(logging_root)
    rows = conn.execute(
        """SELECT p.prompt_id, p.session_id, p.ts, p.seq, p.text, p.words, p.dictated,
                  p.gap_seconds, p.git_branch, p.effort, p.model,
                  COALESCE(SUM(CASE WHEN t.is_sidechain=0 THEN t.weighted END), 0),
                  COALESCE(SUM(CASE WHEN t.is_sidechain=1 THEN t.weighted END), 0),
                  COUNT(t.request_id)
           FROM prompts p LEFT JOIN turns t ON t.prompt_id = p.prompt_id
           WHERE p.text IS NOT NULL AND p.text != ''
             AND COALESCE(p.is_synthetic, 0) = 0
           GROUP BY p.prompt_id
           ORDER BY p.ts DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    rows = _drop_resubmits(rows)

    lines = [_HEADER.format(slug=slug)]
    for (_pid, sid, ts, seq, text, words, dictated, gap, branch, effort, model,
         w_main, w_sub, nturns) in rows:
        stamp = ts
        try:
            stamp = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
            stamp = stamp.strftime("%Y-%m-%d %H:%M %Z")
        except Exception:
            pass
        meta = [f"prompt {seq}"]
        if model:
            meta.append(model.replace("claude-", ""))
        if effort:
            meta.append(f"effort {effort}")
        if branch and branch != "HEAD":
            meta.append(branch)
        if gap is not None:
            meta.append(f"gap {gap // 60}m" if gap >= 60 else f"gap {gap}s")
        meta.append(f"{words} words{' (dictated)' if dictated else ''}")
        cost = f"{nturns} turns / {w_main:,} ITE"
        if w_sub:
            cost += f" + {w_sub:,} ITE subagents"
        lines += [
            "---",
            "",
            f"## {stamp} · {sid[:8]}",
            "",
            f"*{' · '.join(meta)} · {cost}*",
            "",
            text.strip(),
            "",
        ]

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n")
    return len(rows)

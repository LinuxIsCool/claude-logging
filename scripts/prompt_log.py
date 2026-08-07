#!/usr/bin/env python3
"""Prompt-log CLI for claude-logging.

    backfill  ingest every existing transcript into logging.db (idempotent)
    usage     rolling-window token totals across all projects
    render    write the reverse-chronological prompt feed as markdown

The feed is generated, never hand-maintained, so it can always be rebuilt from
the transcripts rather than drifting away from them.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from lib.token_meter import open_db, scan_transcript  # noqa: E402

PROJECTS = Path.home() / ".claude" / "projects"
LOGGING = Path.home() / ".claude" / "local" / "logging"
WINDOWS = {"5h": timedelta(hours=5), "7d": timedelta(days=7), "30d": timedelta(days=30)}


def project_slugs(only: str | None = None):
    for d in sorted(PROJECTS.iterdir()):
        if d.is_dir() and (only is None or d.name == only):
            yield d.name


def cmd_backfill(args):
    total_turns = 0
    for slug in project_slugs(args.project):
        conn = open_db(LOGGING / slug)
        n = 0
        # Main transcripts first: subagent turns attribute to a prompt, and
        # prompts only exist once the parent session has been scanned.
        for tp in sorted((PROJECTS / slug).glob("*.jsonl")):
            n += scan_transcript(conn, tp.stem, str(tp))
        for tp in sorted((PROJECTS / slug).glob("*/subagents/*.jsonl")):
            n += scan_transcript(conn, tp.parent.parent.name, str(tp), sidechain=True)
        conn.close()
        if n:
            print(f"{slug:<50} {n:>8,} turns")
        total_turns += n
    print(f"{'TOTAL':<50} {total_turns:>8,} turns")


def _fmt(n):
    return f"{n:,}"


def cmd_usage(args):
    now = datetime.now(timezone.utc)
    cutoff = (now - max(WINDOWS.values())).isoformat()
    rows = []
    for slug in project_slugs(args.project):
        db = LOGGING / slug / "db" / "logging.db"
        if not db.is_file():
            continue
        conn = open_db(LOGGING / slug)
        rows += conn.execute(
            "SELECT ts, model, is_sidechain, input_tokens, cache_write, cache_read,"
            " output_tokens, weighted, session_id FROM turns WHERE ts >= ?",
            (cutoff,),
        ).fetchall()
        conn.close()

    out = {"as_of": now.isoformat(), "windows": {}}
    for name, delta in WINDOWS.items():
        c = (now - delta).isoformat()
        sel = [r for r in rows if r[0] >= c]
        agg = {
            "turns": len(sel),
            "sessions": len({r[8] for r in sel}),
            "input": sum(r[3] for r in sel),
            "cache_write": sum(r[4] for r in sel),
            "cache_read": sum(r[5] for r in sel),
            "output": sum(r[6] for r in sel),
            "weighted": sum(r[7] for r in sel),
            "sidechain_weighted": sum(r[7] for r in sel if r[2]),
            "by_model": {},
        }
        agg["total"] = agg["input"] + agg["cache_write"] + agg["cache_read"] + agg["output"]
        for r in sel:
            m = agg["by_model"].setdefault(r[1] or "?", {"turns": 0, "weighted": 0, "output": 0})
            m["turns"] += 1
            m["weighted"] += r[7]
            m["output"] += r[6]
        out["windows"][name] = agg

    if args.json:
        print(json.dumps(out, indent=2))
        return

    print(f"as of {now.astimezone().strftime('%Y-%m-%d %H:%M %Z')}")
    for name in WINDOWS:
        a = out["windows"][name]
        share = (100 * a["sidechain_weighted"] / a["weighted"]) if a["weighted"] else 0
        print(f"\n[{name}] {a['turns']:,} turns / {a['sessions']:,} sessions")
        print(f"  input {_fmt(a['input']):>13}   cache write {_fmt(a['cache_write']):>15}")
        print(f"  output{_fmt(a['output']):>13}   cache read  {_fmt(a['cache_read']):>15}")
        print(f"  total {_fmt(a['total']):>13}   weighted    {_fmt(a['weighted']):>15}")
        print(f"  subagent share of weighted: {share:.1f}%")
        for model, m in sorted(a["by_model"].items(), key=lambda kv: -kv[1]["weighted"]):
            print(f"    {model:<32} {_fmt(m['weighted']):>14} ITE  ({m['turns']:,} turns)")


def cmd_render(args):
    slug = args.project
    conn = open_db(LOGGING / slug)
    prompts = conn.execute(
        """SELECT p.prompt_id, p.session_id, p.ts, p.seq, p.text, p.words, p.dictated,
                  p.gap_seconds, p.git_branch, p.effort, p.model,
                  COALESCE(SUM(CASE WHEN t.is_sidechain=0 THEN t.weighted END), 0),
                  COALESCE(SUM(CASE WHEN t.is_sidechain=1 THEN t.weighted END), 0),
                  COUNT(t.request_id)
           FROM prompts p LEFT JOIN turns t ON t.prompt_id = p.prompt_id
           WHERE p.text IS NOT NULL AND p.text != ''
           GROUP BY p.prompt_id
           ORDER BY p.ts DESC
           LIMIT ?""",
        (args.limit,),
    ).fetchall()
    conn.close()

    lines = [
        "# Prompt Log",
        "",
        f"Generated from logging.db for `{slug}`. Reverse chronological: newest at top.",
        "",
        "Entry format: `## YYYY-MM-DD HH:MM TZ · <session>` where `<session>` is the first 8 chars",
        "of the Claude Code session UUID, then an italic metadata line, then the prompt verbatim.",
        "The session tag lets one log absorb parallel instances without losing which thread a prompt",
        "belongs to. ITE = input-token-equivalents (cache reads 0.1x, writes 1.25x, output 5x).",
        "",
        "Regenerate with `prompt_log.py render`. Do not hand-edit.",
        "",
    ]
    for (pid, sid, ts, seq, text, words, dictated, gap, branch, effort, model,
         w_main, w_sub, nturns) in prompts:
        dt = ts
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
            dt = dt.strftime("%Y-%m-%d %H:%M %Z")
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
            f"## {dt} · {sid[:8]}",
            "",
            f"*{' · '.join(meta)} · {cost}*",
            "",
            text.strip(),
            "",
        ]

    dest = Path(os.path.expanduser(args.out))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n")
    print(f"wrote {len(prompts)} prompts -> {dest}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backfill")
    b.add_argument("--project", default=None, help="single project slug")
    b.set_defaults(fn=cmd_backfill)

    u = sub.add_parser("usage")
    u.add_argument("--project", default=None)
    u.add_argument("--json", action="store_true")
    u.set_defaults(fn=cmd_usage)

    r = sub.add_parser("render")
    r.add_argument("--project", default="-home-shawn")
    r.add_argument("--limit", type=int, default=200)
    # Defaults to a generated sibling, not the live hand-maintained log. Verify
    # the generated feed matches, then cut over. Make before break.
    r.add_argument("--out", default="~/legion-brain/local/prompts/prompt-log.generated.md")
    r.set_defaults(fn=cmd_render)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

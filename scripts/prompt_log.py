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

from lib.prompt_feed import DEFAULT_FEED, render_feed  # noqa: E402
from lib.token_meter import classify_prompts, open_db, scan_transcript  # noqa: E402

PROJECTS = Path.home() / ".claude" / "projects"
LOGGING = Path.home() / ".claude" / "local" / "logging"
WINDOWS = {"5h": timedelta(hours=5), "7d": timedelta(days=7), "30d": timedelta(days=30)}


def default_slug() -> str:
    """Project slug for the current working tree."""
    root = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    return root.replace("/", "-")


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
        classify_prompts(conn)
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
    n = render_feed(args.project, out=args.out, limit=args.limit)
    print(f"wrote {n} prompts -> {Path(args.out).expanduser()}")


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
    r.add_argument("--project", default=default_slug())
    r.add_argument("--limit", type=int, default=500)
    r.add_argument("--out", default=DEFAULT_FEED)
    r.set_defaults(fn=cmd_render)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

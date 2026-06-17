# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "mcp>=1.0",
# ]
# ///
"""claude-logging MCP server — query the enriched human-prompt corpus.

task-4159 (EPIC 4152). Exposes the Shawn-only prompt corpus and its LLM
enrichment (intent / entities / sentiment / open_loop) + session titles to any
agent or brief. Read-only.

Tables/views (built by tasks 4155/4158):
  human_prompts (view)  · prompt_enrichment · prompt_entity · session_titles

Run standalone:  uv run --directory <plugin-root> scripts/mcp_server.py
Registered via .mcp.json.
Env: LOGGING_DB  — override DB path.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from mcp.server.fastmcp import FastMCP

DB = Path(
    os.environ.get(
        "LOGGING_DB",
        str(Path.home() / ".claude/local/logging/-home-shawn/db/logging.db"),
    )
)
server = FastMCP("claude-logging")


def _con() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


@server.tool()
async def prompts_search(
    query: str = "",
    limit: int = 20,
    intent: str = "",
    venture: str = "",
    person: str = "",
    sentiment: str = "",
    open_loop: bool = False,
    since: str = "",
    until: str = "",
) -> str:
    """Search Shawn's human prompts with rich filters.

    query: FTS5 text match (optional). intent: 3-char code (CMD/QRY/PLN/BST/DBG/
    RCL/DEC/COR/REV/VNT/MTA/SOC/COO). venture/person: canonical entity name.
    sentiment: neutral/determined/frustrated/excited/anxious/playful/tired.
    open_loop: only intentions flagged needing follow-through. since/until: ISO date.
    Returns matching prompts with date, intent, sentiment, and preview.
    """
    where = ["e.provenance='human-typed'"]
    params: list = []
    joins = "events e JOIN prompt_enrichment en ON en.event_id=e.id"
    if query:
        joins = ("events_fts f JOIN events e ON e.id=f.event_id "
                 "JOIN prompt_enrichment en ON en.event_id=e.id")
        where.append("f.content MATCH ?")
        params.append(query)
    if intent:
        where.append("en.intent=?"); params.append(intent.upper())
    if sentiment:
        where.append("en.sentiment=?"); params.append(sentiment.lower())
    if open_loop:
        where.append("en.open_loop=1")
    if since:
        where.append("e.ts>=?"); params.append(since)
    if until:
        where.append("e.ts<=?"); params.append(until)
    for ent, kind in ((venture, "ventures"), (person, "people")):
        if ent:
            where.append(
                "e.id IN (SELECT event_id FROM prompt_entity WHERE kind=? AND canonical=?)"
            )
            params.extend([kind, ent])
    sql = (
        f"SELECT date(e.ts) d, en.intent, en.sentiment, "
        f"substr(replace(e.content,char(10),' '),1,160) preview "
        f"FROM {joins} WHERE {' AND '.join(where)} "
        f"ORDER BY e.ts DESC LIMIT ?"
    )
    params.append(min(limit, 100))
    con = _con()
    try:
        rows = con.execute(sql, params).fetchall()
        head = f"{len(rows)} prompts"
        body = "\n".join(
            f"- {r['d']} [{r['intent']}/{r['sentiment']}] {r['preview']}" for r in rows
        )
        return f"{head}\n{body}" if rows else "(no matching prompts)"
    finally:
        con.close()


@server.tool()
async def prompts_recall(query: str, limit: int = 5) -> str:
    """'Have I asked this before?' — FTS recall of the most relevant past human
    prompts for a topic, newest first, with their intent and outcome hints."""
    con = _con()
    try:
        rows = con.execute(
            "SELECT date(e.ts) d, en.intent, en.open_loop, "
            "substr(replace(e.content,char(10),' '),1,180) preview "
            "FROM events_fts f JOIN events e ON e.id=f.event_id "
            "JOIN prompt_enrichment en ON en.event_id=e.id "
            "WHERE f.content MATCH ? AND e.provenance='human-typed' "
            "ORDER BY e.ts DESC LIMIT ?",
            (query, min(limit, 50)),
        ).fetchall()
        if not rows:
            return f"No prior prompts about '{query}'."
        return "\n".join(
            f"- {r['d']} [{r['intent']}]{' OPEN-LOOP' if r['open_loop'] else ''} {r['preview']}"
            for r in rows
        )
    finally:
        con.close()


@server.tool()
async def prompts_by_entity(entity: str, kind: str = "people", limit: int = 25) -> str:
    """All prompts mentioning a canonical entity. kind: people/ventures/projects/topics."""
    con = _con()
    try:
        rows = con.execute(
            "SELECT date(e.ts) d, en.intent, "
            "substr(replace(e.content,char(10),' '),1,150) preview "
            "FROM prompt_entity pe JOIN events e ON e.id=pe.event_id "
            "JOIN prompt_enrichment en ON en.event_id=e.id "
            "WHERE pe.kind=? AND pe.canonical=? ORDER BY e.ts DESC LIMIT ?",
            (kind, entity, min(limit, 100)),
        ).fetchall()
        if not rows:
            return f"No prompts mention {kind}={entity!r} (try prompts_stats for canonical names)."
        return f"{len(rows)} prompts about {entity}:\n" + "\n".join(
            f"- {r['d']} [{r['intent']}] {r['preview']}" for r in rows
        )
    finally:
        con.close()


@server.tool()
async def open_loops(limit: int = 25) -> str:
    """Substantive stated intentions that show no follow-through (likely dropped).
    LLM open_loop ∩ planning intent ∩ no in-session execution ∩ aged."""
    con = _con()
    try:
        rows = con.execute(
            "SELECT date(en.ts) d, en.intent, en.sentiment, "
            "substr(replace(h.content,char(10),' '),1,160) preview "
            "FROM prompt_enrichment en JOIN human_prompts h ON h.id=en.event_id "
            "WHERE en.open_loop=1 AND en.intent IN ('PLN','BST','DEC') "
            "AND julianday('now')-julianday(en.ts) >= 7 "
            "ORDER BY en.ts ASC LIMIT ?",
            (min(limit, 100),),
        ).fetchall()
        if not rows:
            return "No open loops."
        return "\n".join(
            f"- {r['d']} [{r['intent']}/{r['sentiment']}] {r['preview']}" for r in rows
        )
    finally:
        con.close()


@server.tool()
async def sessions_search(query: str = "", limit: int = 20) -> str:
    """Search titled work sessions by title text (or list recent if no query)."""
    con = _con()
    try:
        if query:
            rows = con.execute(
                "SELECT title, n_prompts FROM session_titles "
                "WHERE title LIKE ? ORDER BY n_prompts DESC LIMIT ?",
                (f"%{query}%", min(limit, 100)),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT title, n_prompts FROM session_titles "
                "ORDER BY created_at DESC LIMIT ?",
                (min(limit, 100),),
            ).fetchall()
        if not rows:
            return "(no sessions)"
        return "\n".join(f"- [{r['n_prompts']:>2}p] {r['title']}" for r in rows)
    finally:
        con.close()


@server.tool()
async def prompts_stats() -> str:
    """Corpus overview: counts, intent distribution, sentiment, top people/ventures."""
    con = _con()
    try:
        def q(sql, *p):
            return con.execute(sql, p).fetchall()
        total = q("SELECT COUNT(*) n FROM human_prompts")[0]["n"]
        intents = q("SELECT intent, COUNT(*) n FROM prompt_enrichment GROUP BY intent ORDER BY n DESC")
        sent = q("SELECT sentiment, COUNT(*) n FROM prompt_enrichment GROUP BY sentiment ORDER BY n DESC")
        ol = q("SELECT COUNT(*) n FROM prompt_enrichment WHERE open_loop=1")[0]["n"]
        people = q("SELECT canonical, COUNT(*) n FROM prompt_entity WHERE kind='people' GROUP BY canonical ORDER BY n DESC LIMIT 8")
        vent = q("SELECT canonical, COUNT(*) n FROM prompt_entity WHERE kind='ventures' GROUP BY canonical ORDER BY n DESC LIMIT 8")
        sess = q("SELECT COUNT(*) n FROM session_titles")[0]["n"]
        return (
            f"Human prompts: {total} · open-loop flagged: {ol} · titled sessions: {sess}\n"
            f"Intents: " + ", ".join(f"{r['intent']} {r['n']}" for r in intents) + "\n"
            f"Sentiment: " + ", ".join(f"{r['sentiment']} {r['n']}" for r in sent) + "\n"
            f"Top people: " + ", ".join(f"{r['canonical']} {r['n']}" for r in people) + "\n"
            f"Top ventures: " + ", ".join(f"{r['canonical']} {r['n']}" for r in vent)
        )
    finally:
        con.close()


if __name__ == "__main__":
    server.run()

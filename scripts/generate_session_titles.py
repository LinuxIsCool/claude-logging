#!/usr/bin/env python3
"""Generate creative session titles in batches using TELUS Gemma."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.session_titles import open_title_db, read_titles  # noqa: E402
from web.claude_web_sessions import list_sessions as list_claude_web_sessions  # noqa: E402

LOGGING_ROOT = Path.home() / ".claude/local/logging"
INDEX_DB = LOGGING_ROOT / "_index/index.db"
TITLE_DB = LOGGING_ROOT / "_index/session-metadata.db"
PROXY_URL = "http://127.0.0.1:8787/v1/chat/completions"
MODEL = "google/gemma-4-31b-it"
PROMPT_VERSION = "editorial-session-card-v2"


def candidates(index_db: Path) -> list[dict[str, str]]:
    con = sqlite3.connect(f"file:{index_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """WITH sessions AS (
              SELECT session_id, MIN(ts) started_at, MAX(ts) updated_at
              FROM events_index GROUP BY session_id
            ) SELECT s.session_id, s.updated_at,
              COALESCE((SELECT content_preview FROM events_index e WHERE e.session_id=s.session_id AND e.type='UserPromptSubmit' AND COALESCE(e.is_synthetic,0)=0 ORDER BY e.ts ASC LIMIT 1),'') first_prompt,
              COALESCE((SELECT content_preview FROM events_index e WHERE e.session_id=s.session_id AND e.type='UserPromptSubmit' AND COALESCE(e.is_synthetic,0)=0 ORDER BY e.ts DESC LIMIT 1),'') last_prompt,
              COALESCE((SELECT content_preview FROM events_index e WHERE e.session_id=s.session_id AND e.type='AssistantResponse' ORDER BY e.ts DESC LIMIT 1),'') last_response
            FROM sessions s ORDER BY s.updated_at DESC"""
        ).fetchall()
        native = [dict(row) for row in rows]
    finally:
        con.close()
    known = {row["session_id"] for row in native}
    imported = []
    for row in list_claude_web_sessions(10_000, 0):
        session_id = row.get("session_id")
        if not session_id or session_id in known:
            continue
        imported.append({
            "session_id": session_id,
            "updated_at": row.get("updated_at") or "",
            "first_prompt": row.get("opening_prompt") or row.get("title") or "",
            "last_prompt": row.get("opening_prompt") or row.get("title") or "",
            "last_response": row.get("description") or "",
        })
    return native + imported


def source_hash(item: dict[str, str]) -> str:
    material = "\n".join(item.get(key, "") for key in ("first_prompt", "last_prompt", "last_response"))
    return hashlib.sha256(material.encode()).hexdigest()


def request_titles(batch: list[dict[str, str]], proxy_url: str, model: str) -> dict[str, dict[str, str]]:
    records = [{"id": item["session_id"], "first": item["first_prompt"][:700], "last_user": item["last_prompt"][:700], "last_agent": item["last_response"][:700]} for item in batch]
    prompt = (
        "You are the editorial archivist for Legion, naming and describing human-AI work sessions as collectible story cards. "
        "For each record write: (1) a vivid, specific title of 3-9 words naming the actual artifact, goal, turning point, or discovery; "
        "preserve strong names already present and avoid generic phrases like 'Discussion About'; and (2) a factual 1-3 sentence description "
        "that tells the session's story, prioritizing introductions, motivation, background/context, current state, intention, work accomplished, "
        "and conclusions when supported by the evidence. Do not invent facts or merely repeat the title. Return ONLY a JSON object mapping each "
        "exact id to {\"title\":\"...\",\"description\":\"...\"}.\n\n"
        + json.dumps(records, ensure_ascii=False)
    )
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.5, "max_tokens": max(1800, len(batch) * 240), "stream": False}).encode()
    req = urllib.request.Request(proxy_url, data=body, headers={"Content-Type": "application/json", "Authorization": "Bearer local-proxy"})
    with urllib.request.urlopen(req, timeout=240) as response:
        payload = json.load(response)
    content = payload["choices"][0]["message"]["content"].strip()
    match = re.search(r"\{[\s\S]*\}", content)
    if not match:
        raise ValueError(f"Gemma returned no JSON object: {content[:200]}")
    parsed = json.loads(match.group(0))
    result = {}
    for key, value in parsed.items():
        if not isinstance(value, dict):
            value = {"title": str(value), "description": ""}
        result[str(key)] = {
            "title": str(value.get("title") or "").strip().strip('"').rstrip(".!?")[:100],
            "description": str(value.get("description") or "").strip()[:900],
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--proxy-url", default=PROXY_URL)
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args()
    rows = candidates(INDEX_DB)
    existing = read_titles(TITLE_DB)
    pending = [
        row for row in rows
        if args.refresh
        or row["session_id"] not in existing
        or existing[row["session_id"]]["source_hash"] != source_hash(row)
        or existing[row["session_id"]].get("prompt_version") != PROMPT_VERSION
    ]
    if args.limit is not None:
        pending = pending[: args.limit]
    print(json.dumps({"discovered": len(rows), "existing": len(existing), "pending": len(pending), "model": args.model}))
    con = open_title_db(TITLE_DB)
    generated = 0
    try:
        for start in range(0, len(pending), args.batch_size):
            batch = pending[start:start + args.batch_size]
            titles = request_titles(batch, args.proxy_url, args.model)
            for item in batch:
                editorial = titles.get(item["session_id"])
                if not editorial or not editorial["title"]:
                    continue
                con.execute(
                    "INSERT INTO session_titles(session_id,title,description,model,prompt_version,source_hash) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET title=excluded.title,description=excluded.description,model=excluded.model,prompt_version=excluded.prompt_version,source_hash=excluded.source_hash,generated_at=CURRENT_TIMESTAMP",
                    (item["session_id"], editorial["title"], editorial["description"], args.model, PROMPT_VERSION, source_hash(item)),
                )
                generated += 1
            con.commit()
            print(json.dumps({"generated": generated, "attempted": min(start + len(batch), len(pending)), "total": len(pending)}), flush=True)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

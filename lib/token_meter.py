"""Token accounting for claude-logging.

Hooks never receive token counts. What they do receive is `transcript_path`,
and the transcript records `message.usage` on every assistant turn. This module
scans that transcript incrementally and lands two tables alongside the existing
`events` table:

    turns    — one row per API request, keyed by requestId (idempotent upsert)
    prompts  — one row per user prompt, with the metadata a feed wants

Attribution is positional. Assistant turns carry no promptId, so a forward scan
carries the most recent user prompt id and assigns turns to it. Sidechain
(subagent) turns stay interleaved in transcript order, so they attribute to the
prompt that launched them, and are flagged so a reader can separate the cost of
the conversation from the cost of the fan-out it triggered.

Scanning resumes from a stored byte offset, so the cost of a Stop hook is
proportional to the bytes written since the previous Stop, not to session size.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Input-token-equivalents. Cache reads are ~0.1x a fresh input token, cache
# writes ~1.25x, output ~5x. Raw token totals are dominated by cache reads and
# so overstate what a rolling quota actually meters.
W_INPUT = 1.0
W_CACHE_WRITE = 1.25
W_CACHE_READ = 0.1
W_OUTPUT = 5.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    request_id     TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    prompt_id      TEXT,
    ts             TIMESTAMP NOT NULL,
    model          TEXT,
    is_sidechain   INTEGER DEFAULT 0,
    input_tokens   INTEGER DEFAULT 0,
    cache_write    INTEGER DEFAULT 0,
    cache_read     INTEGER DEFAULT 0,
    output_tokens  INTEGER DEFAULT 0,
    weighted       INTEGER DEFAULT 0,
    service_tier   TEXT,
    stop_reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_prompt  ON turns(prompt_id);
CREATE INDEX IF NOT EXISTS idx_turns_ts      ON turns(ts);

CREATE TABLE IF NOT EXISTS prompts (
    prompt_id       TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    ts              TIMESTAMP NOT NULL,
    seq             INTEGER,
    text            TEXT,
    chars           INTEGER DEFAULT 0,
    words           INTEGER DEFAULT 0,
    dictated        INTEGER DEFAULT 0,
    gap_seconds     INTEGER,
    cwd             TEXT,
    git_branch      TEXT,
    prompt_source   TEXT,
    permission_mode TEXT,
    effort          TEXT,
    model           TEXT
);
CREATE INDEX IF NOT EXISTS idx_prompts_session ON prompts(session_id);
CREATE INDEX IF NOT EXISTS idx_prompts_ts      ON prompts(ts);

-- Keyed by transcript file, not by session: a session owns its main transcript
-- plus one file per subagent under <session-id>/subagents/.
CREATE TABLE IF NOT EXISTS meter_state (
    scan_key    TEXT PRIMARY KEY,
    session_id  TEXT,
    offset      INTEGER DEFAULT 0,
    last_prompt TEXT,
    updated_at  TIMESTAMP
);
"""


def open_db(storage_path: Path) -> sqlite3.Connection:
    """Open the project's logging.db with the meter tables present."""
    db_path = Path(storage_path) / "db" / "logging.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def weigh(inp: int, cw: int, cr: int, out: int) -> int:
    return int(W_INPUT * inp + W_CACHE_WRITE * cw + W_CACHE_READ * cr + W_OUTPUT * out)


# Filler and repair markers that survive speech-to-text but almost never appear
# in typed input. Shawn's dictation reliably carries these.
_DISFLUENCY = re.compile(
    r"\b(um|uh|erm|uhh|hmm)\b|\b(\w+)[ ,]+\2\b|\.\.\.\s*of\b",
    re.IGNORECASE,
)


def looks_dictated(text: str) -> bool:
    """Heuristic: was this prompt spoken rather than typed?

    `promptSource` is "typed" for dictated input too, because speech-to-text
    lands as keystrokes. So the only signal is the shape of the prose.

    TODO(shawn): tune this. Candidate signals, in rough order of how much they
    seem to discriminate on your actual input:
      - filler tokens (um, uh, erm) — near-zero false positives, but you edit
        some of them out, so recall is the weak side
      - immediate word repetition ("the the", "in my ear ear")
      - long single-paragraph runs with no newline and no markdown
      - absence of backticks, code fences, or file paths
      - very high word count with very few punctuation marks per word
    Returns True if spoken.
    """
    if not text:
        return False
    if _DISFLUENCY.search(text):
        return True
    # Long, unbroken, punctuation-light prose with no code markers.
    words = text.split()
    if len(words) > 120 and "\n" not in text.strip() and "`" not in text:
        return True
    return False


def _parse_ts(s: str):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:
        return None


def scan_transcript(
    conn: sqlite3.Connection,
    session_id: str,
    transcript_path: str,
    sidechain: bool = False,
) -> int:
    """Ingest new transcript bytes into `turns`. Returns rows written.

    Resumes from the stored byte offset. If the transcript shrank (rewind,
    rotation) the offset resets to 0 and the whole file is re-read; the
    requestId primary key makes that a no-op for rows already present.

    `sidechain=True` marks every turn in the file as subagent spend. Subagent
    transcripts live in their own file under <session-id>/subagents/ and carry
    no isSidechain flag of their own, so the caller supplies it from the path.
    Those files also carry no promptId, so turns attribute to whichever prompt
    of the parent session was most recent at the time.
    """
    path = Path(os.path.expanduser(transcript_path or ""))
    if not path.is_file():
        return 0

    scan_key = str(path)
    row = conn.execute(
        "SELECT offset, last_prompt FROM meter_state WHERE scan_key = ?", (scan_key,)
    ).fetchone()
    offset, current_prompt = (row[0], row[1]) if row else (0, None)

    size = path.stat().st_size
    if size < offset:
        offset, current_prompt = 0, None
    if size == offset:
        return 0

    written = 0
    with open(path, "r", errors="replace") as fh:
        fh.seek(offset)
        for line in fh:
            if not line.endswith("\n"):
                # Partial trailing line: stop before it and re-read next time.
                break
            offset += len(line.encode("utf-8", "replace"))
            try:
                d = json.loads(line)
            except Exception:
                continue

            if d.get("promptId") and not sidechain:
                current_prompt = d["promptId"]
                _prompt_from_transcript(conn, session_id, d)

            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            req = d.get("requestId") or d.get("uuid")
            if not req:
                continue

            ts = d.get("timestamp") or ""
            if sidechain:
                current_prompt = _prompt_at(conn, session_id, ts)

            inp = usage.get("input_tokens", 0) or 0
            cw = usage.get("cache_creation_input_tokens", 0) or 0
            cr = usage.get("cache_read_input_tokens", 0) or 0
            out = usage.get("output_tokens", 0) or 0

            conn.execute(
                """INSERT INTO turns (request_id, session_id, prompt_id, ts, model,
                       is_sidechain, input_tokens, cache_write, cache_read,
                       output_tokens, weighted, service_tier, stop_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(request_id) DO UPDATE SET
                       input_tokens=excluded.input_tokens,
                       cache_write=excluded.cache_write,
                       cache_read=excluded.cache_read,
                       output_tokens=excluded.output_tokens,
                       weighted=excluded.weighted,
                       stop_reason=excluded.stop_reason""",
                (
                    req,
                    session_id,
                    current_prompt,
                    ts,
                    msg.get("model"),
                    1 if (sidechain or d.get("isSidechain")) else 0,
                    inp,
                    cw,
                    cr,
                    out,
                    weigh(inp, cw, cr, out),
                    usage.get("service_tier"),
                    msg.get("stop_reason") or d.get("stopReason"),
                ),
            )
            written += 1

    conn.execute(
        """INSERT INTO meter_state (scan_key, session_id, offset, last_prompt, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(scan_key) DO UPDATE SET
               offset=excluded.offset,
               last_prompt=excluded.last_prompt,
               updated_at=excluded.updated_at""",
        (scan_key, session_id, offset, current_prompt, datetime.now(timezone.utc).isoformat()),
    )
    _refresh_session_tokens(conn, session_id)
    conn.commit()
    return written


def _prompt_at(conn: sqlite3.Connection, session_id: str, ts: str) -> str | None:
    """The parent session's most recent prompt at time `ts`.

    Subagent transcripts carry no promptId, so a fan-out's cost is attributed to
    whichever prompt was in flight when the agent ran.
    """
    row = conn.execute(
        "SELECT prompt_id FROM prompts WHERE session_id = ? AND ts <= ?"
        " ORDER BY ts DESC LIMIT 1",
        (session_id, ts),
    ).fetchone()
    return row[0] if row else None


def _prompt_from_transcript(conn: sqlite3.Connection, session_id: str, d: dict) -> None:
    """Record a prompt seen in the transcript.

    The UserPromptSubmit hook writes a richer row (permission_mode, effort) at
    submit time and wins on conflict. This path is what makes backfill of
    sessions that predate the hook possible, and what heals a prompt whose hook
    failed to fire.
    """
    content = (d.get("message") or {}).get("content")
    if not isinstance(content, str):
        return
    ts = d.get("timestamp") or ""

    prev = conn.execute(
        "SELECT ts, seq FROM prompts WHERE session_id = ? ORDER BY seq DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    gap, seq = None, 1
    if prev:
        seq = (prev[1] or 0) + 1
        a, b = _parse_ts(prev[0]), _parse_ts(ts)
        if a and b:
            gap = int((b - a).total_seconds())

    conn.execute(
        """INSERT INTO prompts (prompt_id, session_id, ts, seq, text, chars, words,
               dictated, gap_seconds, cwd, git_branch, prompt_source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(prompt_id) DO NOTHING""",
        (
            d["promptId"],
            session_id,
            ts,
            seq,
            content,
            len(content),
            len(content.split()),
            1 if looks_dictated(content) else 0,
            gap,
            d.get("cwd"),
            d.get("gitBranch"),
            d.get("promptSource"),
        ),
    )


def _refresh_session_tokens(conn: sqlite3.Connection, session_id: str) -> None:
    """Populate sessions.total_tokens, which has been a dark column since v1."""
    total = conn.execute(
        "SELECT COALESCE(SUM(input_tokens + cache_write + cache_read + output_tokens), 0)"
        " FROM turns WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]
    conn.execute("UPDATE sessions SET total_tokens = ? WHERE id = ?", (total, session_id))


def record_prompt(conn: sqlite3.Connection, payload: dict, git_branch: str | None = None) -> bool:
    """Write one `prompts` row from a UserPromptSubmit hook payload."""
    prompt_id = payload.get("prompt_id")
    session_id = payload.get("session_id")
    if not prompt_id or not session_id:
        return False

    text = payload.get("prompt") or ""
    now = datetime.now(timezone.utc)

    prev = conn.execute(
        "SELECT ts, seq FROM prompts WHERE session_id = ? ORDER BY seq DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    gap, seq = None, 1
    if prev:
        seq = (prev[1] or 0) + 1
        prev_ts = _parse_ts(prev[0])
        if prev_ts:
            gap = int((now - prev_ts).total_seconds())

    effort = payload.get("effort")
    if isinstance(effort, dict):
        effort = effort.get("level")

    conn.execute(
        """INSERT INTO prompts (prompt_id, session_id, ts, seq, text, chars, words,
               dictated, gap_seconds, cwd, git_branch, prompt_source,
               permission_mode, effort, model)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(prompt_id) DO NOTHING""",
        (
            prompt_id,
            session_id,
            now.isoformat(),
            seq,
            text,
            len(text),
            len(text.split()),
            1 if looks_dictated(text) else 0,
            gap,
            payload.get("cwd"),
            git_branch,
            payload.get("prompt_source") or "typed",
            payload.get("permission_mode"),
            effort,
            payload.get("model"),
        ),
    )
    conn.commit()
    return True

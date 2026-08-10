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
CREATE INDEX IF NOT EXISTS idx_prompts_text    ON prompts(session_id, text);

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
    # Additive migrations. Older DBs predate these columns.
    for col, decl in (
        ("is_synthetic", "INTEGER DEFAULT 0"),
        # --- capture boundary (legion_capture) -------------------------------
        # Stamped by the producer at write time. `kind` records WHAT this row
        # is, `discriminator` records HOW that was decided, so a reader can
        # tell a declaration from a fallback without re-deriving it.
        ("uuid7", "TEXT"),
        ("kind", "TEXT"),
        ("discriminator", "TEXT"),
        ("capture_source", "TEXT"),
        ("captured_at", "TIMESTAMP"),
    ):
        try:
            conn.execute(f"ALTER TABLE prompts ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass  # already present
    # uuid7 is unique per row but cannot be declared UNIQUE by ALTER TABLE, so
    # the index carries the guarantee for migrated databases.
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_prompts_uuid7 "
                     "ON prompts(uuid7) WHERE uuid7 IS NOT NULL")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    _install_capture_guards(conn)
    return conn


def _install_capture_guards(conn: sqlite3.Connection) -> None:
    """Make an unstamped prompt row impossible to insert.

    There are exactly two writers today and both stamp. The guard is for the
    third one somebody adds later: a convention only holds until the next
    capture site, a trigger is checked by the database on every insert
    regardless of who wrote it.

    This is what would have caught `sessions.total_tokens` shipping dark
    through an entire version, and `model`/`effort` being NULL in 2,955 of
    2,955 rows, instead of a year later.

    Never blocks a session. A hook that refuses to run because a guard could
    not be installed is a worse outcome than an unguarded column.
    """
    try:
        from lib import capture
        if not capture.CAPTURE_AVAILABLE:
            return
        capture.guard.install(conn, "prompts", required=["uuid7", "kind"])
    except Exception:
        pass


# Not everything with a promptId is user input. Slash-command
# expansions, hook injections, and the caveat blocks Claude Code wraps around
# local command output all arrive as user turns. They are recorded (nothing is
# silently dropped) but flagged so the feed can exclude them.
_SYNTHETIC = re.compile(
    r"^\s*<(local-command-caveat|command-message|command-name|command-args"
    r"|command-stdout|command-contents|user-prompt-submit-hook|system-reminder"
    r"|task-notification)\b",
    re.IGNORECASE,
)


def is_synthetic(text: str) -> bool:
    return bool(_SYNTHETIC.match(text or ""))


def classify_prompts(conn: sqlite3.Connection) -> int:
    """Recompute is_synthetic over every stored prompt. Idempotent."""
    rows = conn.execute("SELECT prompt_id, text, is_synthetic FROM prompts").fetchall()
    changed = 0
    for pid, text, cur in rows:
        want = 1 if is_synthetic(text) else 0
        if want != (cur or 0):
            conn.execute("UPDATE prompts SET is_synthetic = ? WHERE prompt_id = ?", (want, pid))
            changed += 1
    conn.commit()
    return changed


def weigh(inp: int, cw: int, cr: int, out: int) -> int:
    return int(W_INPUT * inp + W_CACHE_WRITE * cw + W_CACHE_READ * cr + W_OUTPUT * out)


# RETIRED 2026-08-07. `looks_dictated()` guessed speech from the shape of the
# prose -- disfluencies, length, absence of code markers. Measured against the
# corpus it was 40% wrong: 77 of 192 positives sat on rows that structurally
# could not be speech, including `<task-notification>` XML, and it had been
# wrong silently for months.
#
# It is not replaced by a better heuristic. A better heuristic shrinks the
# error and keeps it quiet, and a quiet error is harder to find than a loud
# one. Spoken input is indistinguishable from typed at every capture point
# Claude Code exposes, because speech-to-text lands as keystrokes. Rows are now
# stamped `kind=typed, discriminator=channel|undeclared`, which is what is
# actually known.
#
# The real fix is upstream: the speech-to-text path marking its own output.
# When it does, stamp `Kind.SPOKEN` with `Discriminator.DECLARED` and this
# question is answered by evidence instead of by prose style.


def looks_dictated(text: str) -> bool:
    """Retired. Kept as a tombstone so a caller fails loudly rather than
    silently reintroducing a 40%-wrong guess."""
    raise NotImplementedError(
        "looks_dictated() was retired: it was 40% wrong and silent. Spoken "
        "input is not distinguishable from typed at any capture point Claude "
        "Code exposes. Stamp Kind.SPOKEN only when the STT path declares it."
    )


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


def _seq_and_gap(conn: sqlite3.Connection, session_id: str, ts: str, synth: bool):
    """Next sequence number, and think-time since the previous real prompt.

    `seq` counts every prompt so it stays a faithful transcript position. `gap`
    measures against the previous non-synthetic prompt, because a slash-command
    expansion firing between two of your messages is not thinking time.
    """
    seq_row = conn.execute(
        "SELECT MAX(seq) FROM prompts WHERE session_id = ?", (session_id,)
    ).fetchone()
    seq = (seq_row[0] or 0) + 1
    if synth:
        return seq, None
    prev = conn.execute(
        "SELECT ts FROM prompts WHERE session_id = ? AND COALESCE(is_synthetic,0) = 0"
        " ORDER BY ts DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    gap = None
    if prev:
        a, b = _parse_ts(prev[0]), _parse_ts(ts)
        if a and b:
            gap = int((b - a).total_seconds())
    return seq, gap


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
    synth = is_synthetic(content)
    seq, gap = _seq_and_gap(conn, session_id, ts, synth)

    from lib import capture
    capture.require()
    kind, disc = _classify_transcript_prompt(d, synth, capture)
    stamped = capture.stamp(
        {}, kind=kind, source="transcript", discriminator=disc,
        ts=ts or None, key_parts=(session_id, d["promptId"]),
    )

    conn.execute(
        """INSERT INTO prompts (prompt_id, session_id, ts, seq, text, chars, words,
               dictated, gap_seconds, cwd, git_branch, prompt_source, is_synthetic,
               uuid7, kind, discriminator, capture_source, captured_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(prompt_id) DO NOTHING""",
        (
            d["promptId"],
            session_id,
            ts,
            seq,
            content,
            len(content),
            len(content.split()),
            None,                       # `dictated`: retired, never guessed again
            gap,
            d.get("cwd"),
            d.get("gitBranch"),
            d.get("promptSource"),
            1 if synth else 0,
            stamped["uuid7"],
            stamped["kind"],
            stamped["discriminator"],
            stamped["source"],
            stamped["captured_at"],
        ),
    )


def _classify_transcript_prompt(d: dict, synth: bool, capture):
    """(kind, discriminator) from what the transcript declares.

    Unlike the hook payload, a transcript record often states its own origin.
    Most authoritative declaration first; where nothing is declared the row
    says UNDECLARED rather than inheriting a default that looks like evidence.
    """
    Kind, Disc = capture.Kind, capture.Discriminator
    if synth:
        return Kind.EXPANSION, Disc.CHANNEL

    origin = (d.get("origin") or {}).get("kind")
    source = d.get("promptSource")

    if origin is not None and origin != "human":
        # The harness named a machine origin outright: cron, a task
        # notification, an SDK call. 1,085 such rows once wore the human's
        # identity because nothing recorded this.
        return (Kind.SCHEDULED if origin in ("cron", "agent", "sdk")
                else Kind.INJECTION), Disc.DECLARED

    if source in ("typed", "queued"):
        return (Kind.QUEUED if source == "queued" else Kind.TYPED), (
            Disc.DECLARED if origin == "human" else Disc.CHANNEL)

    if origin == "human":
        return Kind.TYPED, Disc.DECLARED

    # The oldest transcripts declared neither. Say so.
    return Kind.TYPED, Disc.UNDECLARED


def _refresh_session_tokens(conn: sqlite3.Connection, session_id: str) -> None:
    """Populate sessions.total_tokens, which has been a dark column since v1."""
    total = conn.execute(
        "SELECT COALESCE(SUM(input_tokens + cache_write + cache_read + output_tokens), 0)"
        " FROM turns WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]
    conn.execute("UPDATE sessions SET total_tokens = ? WHERE id = ?", (total, session_id))


def record_prompt(conn: sqlite3.Connection, payload: dict, git_branch: str | None = None) -> bool:
    """Write one `prompts` row from a UserPromptSubmit hook payload.

    Provenance is stamped here, at the boundary, because here is the last point
    at which any of it is known. Measured against 2,149 real payloads:

      declared by the payload   session_id, prompt_id, cwd, permission_mode
      declared by the CHANNEL   that a human submitted this — the hook fires
                                on submit and on nothing else
      never present             model (0 of 2,149), effort, prompt_source

    So `kind` is TYPED with `discriminator=CHANNEL`: the channel proves a human
    submitted, and proves nothing about typed-versus-spoken. That distinction
    needs the speech-to-text path to mark its own output, and until it does the
    honest record is TYPED/CHANNEL rather than a guess.

    `prompt_source` used to default to "typed" and `dictated` used to hold the
    output of `looks_dictated()`, which was 40% wrong. Both are now left NULL:
    an absent value is recoverable, a fabricated one is not.
    """
    prompt_id = payload.get("prompt_id")
    session_id = payload.get("session_id")
    if not prompt_id or not session_id:
        return False

    text = payload.get("prompt") or ""
    now = datetime.now(timezone.utc)
    # Preserve the boundary timestamp on the source payload so a retry or a
    # second projection of the same captured record derives the identical v7
    # identity instead of sampling a new clock millisecond.
    capture_ts = payload.get("ts") or payload.setdefault("_capture_ts", now.isoformat())
    synth = is_synthetic(text)
    seq, gap = _seq_and_gap(conn, session_id, str(capture_ts), synth)

    effort = payload.get("effort")
    if isinstance(effort, dict):
        effort = effort.get("level")

    from lib import capture
    capture.require()
    # A synthetic turn rode in on the user channel but nobody submitted it.
    # Saying so at capture time is what makes `SELECT count(*) WHERE kind IN
    # (submit kinds)` the answer to "how many prompts", with no subtraction.
    kind = capture.Kind.EXPANSION if synth else capture.Kind.TYPED
    stamped = capture.stamp(
        {}, kind=kind, source="UserPromptSubmit",
        discriminator=capture.Discriminator.CHANNEL,
        ts=capture_ts, key_parts=(session_id, prompt_id),
    )

    conn.execute(
        """INSERT INTO prompts (prompt_id, session_id, ts, seq, text, chars, words,
               dictated, gap_seconds, cwd, git_branch, prompt_source,
               permission_mode, effort, model, is_synthetic,
               uuid7, kind, discriminator, capture_source, captured_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(prompt_id) DO NOTHING""",
        (
            prompt_id,
            session_id,
            str(capture_ts),
            seq,
            text,
            len(text),
            len(text.split()),
            None,                       # `dictated`: retired, never guessed again
            gap,
            payload.get("cwd"),
            git_branch,
            payload.get("prompt_source"),   # NULL when undeclared, not "typed"
            payload.get("permission_mode"),
            effort,
            payload.get("model"),
            1 if synth else 0,
            stamped["uuid7"],
            stamped["kind"],
            stamped["discriminator"],
            stamped["source"],
            stamped["captured_at"],
        ),
    )
    conn.commit()
    return True

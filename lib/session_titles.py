"""Durable, regenerable model-authored titles for the session catalog."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_titles (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_session_titles_generated_at ON session_titles(generated_at);
"""


def open_title_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    columns = {row[1] for row in con.execute("PRAGMA table_info(session_titles)")}
    if "description" not in columns:
        con.execute("ALTER TABLE session_titles ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    return con


def read_titles(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return {row["session_id"]: dict(row) for row in con.execute("SELECT * FROM session_titles")}
    finally:
        con.close()

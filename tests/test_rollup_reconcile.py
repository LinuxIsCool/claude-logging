"""task-4134 — true reconcile pass recovers sub-watermark / out-of-order events."""
import sqlite3
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts" / "v2"))

import rollup_index  # noqa: E402

INDEX_DDL = (PLUGIN_ROOT / "scripts" / "v2" / "init_cross_project_index.sql").read_text()


def _make_source(path: Path, events: list[dict]) -> None:
    """Create a per-project source logging.db with the given events."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE events (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, "
        "ts TIMESTAMP, agent_session_num INTEGER DEFAULT 0, data JSON, "
        "content TEXT, persona TEXT)"
    )
    con.executemany(
        "INSERT INTO events (id, session_id, type, ts, content, persona) "
        "VALUES (:id, :session_id, :type, :ts, :content, :persona)",
        events,
    )
    con.commit()
    con.close()


def _make_index(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(INDEX_DDL)
    con.commit()
    return con


def _ev(eid, ts, content="hello", typ="UserPromptSubmit", persona=None):
    return {"id": eid, "session_id": "s1", "type": typ, "ts": ts,
            "content": content, "persona": persona}


def test_reconcile_recovers_sub_watermark_event(tmp_path):
    """The exact bug: an event with ts < watermark is skipped by rollup_project
    but recovered by reconcile_project."""
    src = tmp_path / "proj" / "db" / "logging.db"
    _make_source(src, [
        _ev("evt-1", "2026-06-14T01:00:00+00:00"),
        _ev("evt-2", "2026-06-14T02:00:00+00:00"),
    ])
    idx = _make_index(tmp_path / "index.db")

    inserted, max_ts = rollup_index.rollup_project(idx, "proj", src)
    rollup_index.update_rollup_state(idx, "proj", max_ts, inserted)
    idx.commit()
    assert idx.execute("SELECT COUNT(*) FROM events_index").fetchone()[0] == 2

    scon = sqlite3.connect(src)
    scon.execute(
        "INSERT INTO events (id, session_id, type, ts, content) "
        "VALUES ('evt-0', 's1', 'UserPromptSubmit', '2026-06-14T00:30:00+00:00', 'late')"
    )
    scon.commit()
    scon.close()

    inserted2, _ = rollup_index.rollup_project(idx, "proj", src)
    idx.commit()
    assert inserted2 == 0
    assert idx.execute("SELECT COUNT(*) FROM events_index").fetchone()[0] == 2

    recovered, idx_count = rollup_index.reconcile_project(idx, "proj", src)
    idx.commit()
    assert recovered == 1
    assert idx_count == 3
    assert idx.execute(
        "SELECT content_preview FROM events_index WHERE event_id='evt-0'"
    ).fetchone()[0] == "late"


def test_reconcile_idempotent(tmp_path):
    src = tmp_path / "proj" / "db" / "logging.db"
    _make_source(src, [_ev("evt-1", "2026-06-14T01:00:00+00:00")])
    idx = _make_index(tmp_path / "index.db")

    first, _ = rollup_index.reconcile_project(idx, "proj", src)
    idx.commit()
    assert first == 1
    second, _ = rollup_index.reconcile_project(idx, "proj", src)
    idx.commit()
    assert second == 0


def test_reconcile_fast_path_no_drift(tmp_path):
    """Matching counts → 0 inserts, event_count made truthful."""
    src = tmp_path / "proj" / "db" / "logging.db"
    _make_source(src, [_ev("evt-1", "2026-06-14T01:00:00+00:00")])
    idx = _make_index(tmp_path / "index.db")
    rollup_index.reconcile_project(idx, "proj", src)
    idx.commit()

    inserted, idx_count = rollup_index.reconcile_project(idx, "proj", src)
    idx.commit()
    assert inserted == 0
    assert idx_count == 1
    assert idx.execute(
        "SELECT event_count FROM rollup_state WHERE project_slug='proj'"
    ).fetchone()[0] == 1


def test_reconcile_recovered_event_is_searchable(tmp_path):
    src = tmp_path / "proj" / "db" / "logging.db"
    _make_source(src, [_ev("evt-9", "2026-06-14T01:00:00+00:00", content="findme zebra")])
    idx = _make_index(tmp_path / "index.db")
    rollup_index.reconcile_project(idx, "proj", src)
    idx.commit()
    hit = idx.execute(
        "SELECT event_id FROM events_index_fts WHERE events_index_fts MATCH 'zebra'"
    ).fetchone()
    assert hit is not None and hit[0] == "evt-9"

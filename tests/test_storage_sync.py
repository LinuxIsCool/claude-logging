"""Regression tests for the two confirmed data-loss bugs.

IMPORTANT: never assert FTS correctness with `SELECT COUNT(*) FROM events_fts`.
With external content that delegates to the base table and always passes. It is
a false green. Assert with MATCH or 'integrity-check'.
"""

from lib.storage import Event, SQLiteStorage


def _match(db, term):
    return db.conn.execute("SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH ?", (term,)).fetchone()[0]


def test_resyncing_same_event_does_not_duplicate_fts(tmp_path):
    """The confirmed FTS5 duplicate bug. Backfill re-syncs by design."""
    db = SQLiteStorage(tmp_path / "t.db")
    e = Event(
        id="evt-1", session_id="s", type="UserPromptSubmit", ts="2026-07-15T00:00:00+00:00", content="hello world"
    )
    for _ in range(3):
        db.insert_event(e)
    assert _match(db, "hello") == 1
    db.close()


def test_changing_event_content_reindexes(tmp_path):
    """Old term must stop matching; new term must start matching."""
    db = SQLiteStorage(tmp_path / "t.db")
    db.insert_event(Event(id="evt-1", session_id="s", type="T", ts="2026-07-15T00:00:00+00:00", content="hello world"))
    db.insert_event(Event(id="evt-1", session_id="s", type="T", ts="2026-07-15T00:00:00+00:00", content="goodbye moon"))
    assert _match(db, "hello") == 0
    assert _match(db, "goodbye") == 1
    db.close()


def test_empty_and_null_content_do_not_disturb_the_index(tmp_path):
    """The trigger guards mirror the old `if event.content:` behaviour.

    Empty and NULL content yield no searchable terms whether indexed or not, so
    "was it indexed" is not directly observable through MATCH. What IS
    observable, and what matters, is that the guards keep the index internally
    consistent and leave real rows searchable. Verified separately that
    'integrity-check' tolerates trigger-skipped rows and that 'rebuild' agrees
    with the guards.
    """
    db = SQLiteStorage(tmp_path / "t.db")
    db.insert_event(Event(id="evt-1", session_id="s", type="T", ts="2026-07-15T00:00:00+00:00", content=""))
    db.insert_event(Event(id="evt-2", session_id="s", type="T", ts="2026-07-15T00:00:00+00:00", content=None))
    db.insert_event(Event(id="evt-3", session_id="s", type="T", ts="2026-07-15T00:00:00+00:00", content="realterm"))
    assert _match(db, "realterm") == 1
    db.conn.execute("INSERT INTO events_fts(events_fts) VALUES('integrity-check')")
    db.close()


def test_fts_index_integrity_after_resync(tmp_path):
    db = SQLiteStorage(tmp_path / "t.db")
    for i in range(20):
        db.insert_event(
            Event(id=f"evt-{i}", session_id="s", type="T", ts="2026-07-15T00:00:00+00:00", content=f"payload {i}")
        )
    for i in range(20):
        db.insert_event(
            Event(id=f"evt-{i}", session_id="s", type="T", ts="2026-07-15T00:00:00+00:00", content=f"payload {i}")
        )
    db.conn.execute("INSERT INTO events_fts(events_fts) VALUES('integrity-check')")
    assert _match(db, "payload") == 20
    db.close()


def test_search_still_returns_results(tmp_path):
    """The join moved from event_id to rowid; search must still work."""
    db = SQLiteStorage(tmp_path / "t.db")
    db.insert_event(
        Event(
            id="evt-1",
            session_id="s",
            type="UserPromptSubmit",
            ts="2026-07-15T00:00:00+00:00",
            content="unique_token_xyz",
        )
    )
    rows = db.search("unique_token_xyz")
    assert len(rows) == 1
    assert rows[0]["id"] == "evt-1"
    db.close()

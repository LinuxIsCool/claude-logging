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


def test_bare_update_keeps_row_in_index(tmp_path):
    """Regression for the split au_del/au_ins trigger pair.

    SQLite fires AFTER UPDATE triggers in REVERSE creation order, so a split
    pair runs the INSERT trigger first and the DELETE trigger last: the
    delete wins and the row silently leaves the index on ANY bare UPDATE.
    scripts/v2/backfill_001.py issues exactly this kind of UPDATE (setting
    tool_name/tool_input_hash and duration_ms without touching content), so
    this mimics that call shape directly on db.conn rather than going through
    insert_event (which only ever does DELETE + INSERT and therefore never
    exercises the update triggers at all).
    """
    db = SQLiteStorage(tmp_path / "t.db")
    db.insert_event(
        Event(id="evt-1", session_id="s", type="PreToolUse", ts="2026-07-15T00:00:00+00:00", content="alpha")
    )
    assert _match(db, "alpha") == 1
    db.conn.execute("UPDATE events SET tool_name = ? WHERE id = ?", ("Bash", "evt-1"))
    db.conn.commit()
    assert _match(db, "alpha") == 1
    db.close()


def test_bare_update_survives_rebuild_comparison(tmp_path):
    """'integrity-check' does NOT catch a row silently dropped from the index.

    The only oracle that catches it is comparing the live index against a
    fresh rebuild. Insert several events, issue a bare UPDATE like
    scripts/v2/backfill_001.py does, snapshot MATCH counts, rebuild, and
    assert the counts are unchanged. If the live index disagrees with the
    rebuild, the triggers are wrong.
    """
    db = SQLiteStorage(tmp_path / "t.db")
    for i in range(5):
        db.insert_event(
            Event(
                id=f"evt-{i}", session_id="s", type="PreToolUse", ts="2026-07-15T00:00:00+00:00", content=f"payload {i}"
            )
        )
    db.conn.execute("UPDATE events SET tool_name = ? WHERE id = ?", ("Bash", "evt-2"))
    db.conn.execute("UPDATE events SET duration_ms = ? WHERE id = ?", (42, "evt-3"))
    db.conn.commit()

    db.conn.execute("INSERT INTO events_fts(events_fts) VALUES('integrity-check')")

    before = _match(db, "payload")
    assert before == 5

    db.conn.execute("INSERT INTO events_fts(events_fts) VALUES('rebuild')")
    db.conn.commit()

    after = _match(db, "payload")
    assert after == before
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

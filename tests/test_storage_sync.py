"""Regression tests for the two confirmed data-loss bugs.

IMPORTANT: never assert FTS correctness with `SELECT COUNT(*) FROM events_fts`.
With external content that delegates to the base table and always passes. It is
a false green. Assert with MATCH or 'integrity-check'.
"""

import json

from lib.storage import Event, SQLiteStorage, StorageManager


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


def test_torn_line_is_retried_not_dropped(tmp_path):
    """A half-written trailing line must not advance the cursor past it."""
    sm = StorageManager(tmp_path / "logging")
    path = sm.jsonl.get_session_path("s1")
    path.parent.mkdir(parents=True, exist_ok=True)

    good = json.dumps(
        {"id": "evt-1", "session_id": "s1", "type": "T", "ts": "2026-07-15T00:00:00+00:00", "content": "first"}
    )
    path.write_text(good + "\n" + '{"id": "evt-2", "session_id": "s1", "ty')

    assert sm.sync_session("s1") == 1
    cursor = sm.sqlite.get_sync_position("s1")
    assert cursor == len(good) + 1, "cursor must stop at the last complete line"

    # Writer completes the torn line; the event must now be picked up.
    rest = json.dumps(
        {"id": "evt-2", "session_id": "s1", "type": "T", "ts": "2026-07-15T00:00:01+00:00", "content": "second"}
    )
    path.write_text(good + "\n" + rest + "\n")
    assert sm.sync_session("s1") == 1
    ids = {r[0] for r in sm.sqlite.conn.execute("SELECT id FROM events").fetchall()}
    assert ids == {"evt-1", "evt-2"}, "the torn line's event was lost forever"
    sm.close()


def test_cursor_is_byte_accurate_with_unicode(tmp_path):
    """Cursor is a byte offset; the file must be read in binary mode.

    Measured: a plain "two complete lines, first non-ASCII" sync does NOT
    discriminate against text mode on CPython. At a readline() boundary
    right after an ASCII "\\n", the text-mode decoder's state is trivial, so
    its tell() cookie collides with the raw byte offset for UTF-8 -- verified
    directly against the pre-Task-5 code (which used stat().st_size, not
    tell()) and it round-trips fine too. That construction is not a real
    regression guard; see task-5-report.md for the three-way check.

    The real, provable divergence: a write torn in the MIDDLE of a
    multi-byte UTF-8 character (not at a line boundary) -- the realistic
    shape of a torn write when content contains emoji. In binary mode this
    is just another torn tail: no trailing newline, so we stop and retry,
    cursor parked exactly at the end of line 1. In text mode, readline()
    itself must decode as it reads and raises UnicodeDecodeError on the
    incomplete codepoint -- an uncaught exception, not a graceful retry.
    Confirmed by temporarily flipping open() to text mode (see
    task-5-report.md): this test fails there (errors) and passes here.
    """
    sm = StorageManager(tmp_path / "logging")
    path = sm.jsonl.get_session_path("s2")
    path.parent.mkdir(parents=True, exist_ok=True)
    line1 = json.dumps(
        {
            "id": "evt-1",
            "session_id": "s2",
            "type": "T",
            "ts": "2026-07-15T00:00:00+00:00",
            "content": "emoji 🔥 and accents éàü",
        },
        ensure_ascii=False,
    )
    path.write_text(line1 + "\n", encoding="utf-8")

    assert sm.sync_session("s2") == 1
    pos_after_line1 = path.stat().st_size
    assert sm.sqlite.get_sync_position("s2") == pos_after_line1

    # Torn write: complete JSON prefix + only the first 2 of the emoji's 4
    # UTF-8 bytes, no trailing newline. The writer has not finished this line.
    line2_full = json.dumps(
        {"id": "evt-2", "session_id": "s2", "type": "T", "ts": "2026-07-15T00:00:01+00:00", "content": "fire 🔥"},
        ensure_ascii=False,
    ).encode("utf-8")
    emoji_start = line2_full.index("🔥".encode())
    torn_prefix = line2_full[: emoji_start + 2]
    remainder = line2_full[emoji_start + 2 :]

    with open(path, "ab") as f:
        f.write(torn_prefix)

    assert sm.sync_session("s2") == 0, "torn mid-character write must not be parsed yet"
    assert sm.sqlite.get_sync_position("s2") == pos_after_line1, "cursor must not advance into torn multi-byte bytes"

    # Writer completes the character and the line.
    with open(path, "ab") as f:
        f.write(remainder + b"\n")

    assert sm.sync_session("s2") == 1, "evt-2 must land once the writer completes the line"
    ids = {r[0] for r in sm.sqlite.conn.execute("SELECT id FROM events").fetchall()}
    assert ids == {"evt-1", "evt-2"}
    assert sm.sqlite.get_sync_position("s2") == path.stat().st_size
    sm.close()


def test_permanently_invalid_line_is_skipped_not_stalled(tmp_path):
    """A newline-terminated line that can never parse must be skipped, not stalled on.

    file = [good, permanently-invalid, good]. A flock-protected writer only
    ever emits whole newline-terminated lines, so a newline-terminated line
    that will not parse is genuinely corrupt, not in-flight; waiting for it
    to become valid is futile. Both good events must land, the cursor must
    reach EOF, and a second sync must find nothing left to do.

    This MUST FAIL against the break-on-unparseable code: that code breaks on
    the invalid line and never advances past it, so evt-2 is permanently
    unreachable and the second sync also returns 0 forever.
    """
    sm = StorageManager(tmp_path / "logging")
    path = sm.jsonl.get_session_path("s3")
    path.parent.mkdir(parents=True, exist_ok=True)

    good1 = json.dumps(
        {"id": "evt-1", "session_id": "s3", "type": "T", "ts": "2026-07-15T00:00:00+00:00", "content": "first"}
    )
    invalid = "not valid json at all"
    good2 = json.dumps(
        {"id": "evt-2", "session_id": "s3", "type": "T", "ts": "2026-07-15T00:00:01+00:00", "content": "second"}
    )
    path.write_text(good1 + "\n" + invalid + "\n" + good2 + "\n")

    synced = sm.sync_session("s3")
    ids = {r[0] for r in sm.sqlite.conn.execute("SELECT id FROM events").fetchall()}
    assert ids == {"evt-1", "evt-2"}, "both good events must land; the corrupt line must be skipped, not stalled on"
    assert synced == 2

    assert sm.sqlite.get_sync_position("s3") == path.stat().st_size, "cursor must reach EOF after skipping"
    assert sm.sync_session("s3") == 0, "a second sync must find nothing left to do"
    sm.close()


def test_corrupt_line_count_is_exposed(tmp_path):
    """StorageManager.last_sync_corrupt_lines counts skipped-corrupt lines, reset per sync."""
    sm = StorageManager(tmp_path / "logging")

    clean_path = sm.jsonl.get_session_path("clean")
    clean_path.parent.mkdir(parents=True, exist_ok=True)
    clean_path.write_text(
        json.dumps({"id": "evt-1", "session_id": "clean", "type": "T", "ts": "2026-07-15T00:00:00+00:00"}) + "\n"
    )
    sm.sync_session("clean")
    assert sm.last_sync_corrupt_lines == 0

    dirty_path = sm.jsonl.get_session_path("dirty")
    dirty_path.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps({"id": "evt-2", "session_id": "dirty", "type": "T", "ts": "2026-07-15T00:00:00+00:00"})
    good2 = json.dumps({"id": "evt-3", "session_id": "dirty", "type": "T", "ts": "2026-07-15T00:00:01+00:00"})
    dirty_path.write_text(good + "\n" + "not valid json\n" + good2 + "\n")
    sm.sync_session("dirty")
    assert sm.last_sync_corrupt_lines == 1
    sm.close()

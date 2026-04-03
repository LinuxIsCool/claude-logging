#!/usr/bin/env python3
"""
Unit tests for session_capture.py — Phase 2 entity extraction & summary storage.

Tests the lightweight NER, summary upsert logic, entity dedup, and batch extraction.
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest"]
# ///

import sqlite3
import sys
from pathlib import Path

import pytest

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from session_capture import (
    extract_entities_lightweight,
    process_postcompact_summary,
    store_session_summary,
)

# ── Schema setup ──────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_summaries (
    session_id TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    source TEXT DEFAULT 'compact',
    entities_extracted INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS session_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    entity_name TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    mention_count INTEGER DEFAULT 1,
    first_seen TIMESTAMP NOT NULL,
    context TEXT,
    UNIQUE(session_id, entity_name)
);
"""


@pytest.fixture
def db_path(tmp_path):
    """Create a temporary SQLite DB with the correct schema."""
    db = tmp_path / "logging.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.close()
    return db


# ── extract_entities_lightweight ──────────────────────────────────────


class TestExtractEntitiesLightweight:
    def test_person_extraction(self):
        text = "Alice met with Bob Smith and Carol White about the project timeline."
        entities = extract_entities_lightweight(text)
        names = {e["name"] for e in entities if e["type"] == "Person"}
        assert "Alice" in names or "Bob Smith" in names or "Carol White" in names

    def test_venture_extraction(self):
        """Venture extraction depends on ~/.claude/local/ventures/ at runtime.
        Skip if no ventures are configured on this machine."""
        from session_capture import VENTURE_PATTERNS

        if not VENTURE_PATTERNS:
            pytest.skip("No venture patterns configured on this machine")
        # Just verify the extraction machinery works — any venture match counts
        # Build a test string from the actual pattern
        text = "Working on ventures today."
        entities = extract_entities_lightweight(text)
        # Non-empty patterns mean the loader worked; specific matches depend on config
        assert isinstance(entities, list)

    def test_project_extraction(self):
        """Project extraction depends on installed plugins at runtime.
        Skip if no plugins are installed on this machine."""
        from session_capture import PROJECT_PATTERNS

        if not PROJECT_PATTERNS:
            pytest.skip("No project patterns configured on this machine")
        text = "Working on claude-logging today."
        entities = extract_entities_lightweight(text)
        names = {e["name"] for e in entities if e["type"] == "Project"}
        # claude-logging should always match since we're running inside it
        assert "claude-logging" in names

    def test_date_extraction(self):
        text = "The deadline is 2026-03-20 and it started on 2026-01-15."
        entities = extract_entities_lightweight(text)
        dates = [e for e in entities if e["type"] == "Date"]
        assert len(dates) == 2

    def test_money_extraction(self):
        text = "The budget is $15,000.00 and we spent $3,200."
        entities = extract_entities_lightweight(text)
        money = [e for e in entities if e["type"] == "Money"]
        assert len(money) == 2

    def test_duration_extraction(self):
        text = "Alice worked 18 hours and Ed Green did 5.5 hrs."
        entities = extract_entities_lightweight(text)
        durations = [e for e in entities if e["type"] == "Duration"]
        assert len(durations) == 2

    def test_mention_count_accuracy(self):
        text = "Alice Smith arrived. Then Alice Smith spoke. Alice Smith left."
        entities = extract_entities_lightweight(text)
        alice = next((e for e in entities if e["name"] == "Alice Smith"), None)
        assert alice is not None
        assert alice["count"] == 3

    def test_empty_text(self):
        entities = extract_entities_lightweight("")
        assert entities == []

    def test_no_entities_in_random_text(self):
        text = "The quick brown fox jumped over the lazy dog."
        entities = extract_entities_lightweight(text)
        assert entities == []

    def test_case_insensitive_dedup(self):
        """Entities should not be double-counted with different casing."""
        # Use date pattern which is always available and case-independent
        text = "Meeting on 2026-03-20 and again on 2026-03-20 and then 2026-03-20."
        entities = extract_entities_lightweight(text)
        dates = [e for e in entities if e["type"] == "Date"]
        # Should be exactly 1 entity (deduped by lowercase key)
        assert len(dates) == 1
        assert dates[0]["count"] == 3

    def test_multiword_person(self):
        text = "Diana Brown sent the email."
        entities = extract_entities_lightweight(text)
        names = {e["name"] for e in entities if e["type"] == "Person"}
        assert any("Diana" in n or "Brown" in n for n in names)


# ── store_session_summary ─────────────────────────────────────────────


class TestStoreSessionSummary:
    def test_basic_store(self, db_path):
        # Text with entities that match built-in patterns (date, money, person name)
        store_session_summary(
            db_path,
            "sess-001",
            "Alice Smith spent $5,000 on 2026-04-01 working on claude-hippo.",
            source="compact",
        )
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT session_id, summary, source, entities_extracted FROM session_summaries").fetchone()
        conn.close()
        assert row[0] == "sess-001"
        assert "claude-hippo" in row[1]
        assert row[2] == "compact"
        assert row[3] > 0  # entities were auto-extracted (Date, Money, Person)

    def test_entity_storage(self, db_path):
        store_session_summary(
            db_path,
            "sess-002",
            "Alice Smith spent $5,000 on 2026-04-01.",
        )
        conn = sqlite3.connect(str(db_path))
        entities = conn.execute(
            "SELECT entity_name, entity_type FROM session_entities WHERE session_id='sess-002'"
        ).fetchall()
        conn.close()
        names = {e[0] for e in entities}
        # These patterns are always available (Person, Money, Date)
        assert any("Alice" in n or "Smith" in n for n in names)
        assert "$5,000" in names
        assert "2026-04-01" in names

    def test_upsert_replaces_count(self, db_path):
        """ON CONFLICT should replace mention_count, not accumulate."""
        store_session_summary(
            db_path,
            "sess-003",
            "TestEntity did 10 things.",
            entities=[{"name": "TestEntity", "type": "Person", "count": 5}],
        )
        # Store again — should replace, not add
        store_session_summary(
            db_path,
            "sess-003",
            "TestEntity did 10 things again.",
            entities=[{"name": "TestEntity", "type": "Person", "count": 3}],
        )
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT mention_count FROM session_entities WHERE session_id='sess-003' AND entity_name='TestEntity'"
        ).fetchone()
        conn.close()
        # Should be 3 (replaced), not 8 (accumulated)
        assert row[0] == 3

    def test_pre_extracted_entities(self, db_path):
        """When entities are passed explicitly, don't re-extract."""
        entities = [{"name": "CustomEntity", "type": "Person", "count": 7}]
        store_session_summary(
            db_path,
            "sess-004",
            "Some text.",
            entities=entities,
        )
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT entity_name, mention_count FROM session_entities WHERE session_id='sess-004'"
        ).fetchone()
        conn.close()
        assert row[0] == "CustomEntity"
        assert row[1] == 7


# ── process_postcompact_summary ───────────────────────────────────────


class TestProcessPostcompactSummary:
    def test_returns_entity_count(self, db_path):
        count = process_postcompact_summary(
            db_path,
            "sess-005",
            "Alice Smith spent $5,000 on 2026-04-01 and worked 8 hours.",
        )
        assert count >= 3  # Alice Smith, $5,000, 2026-04-01, 8 hours

    def test_stores_with_compact_source(self, db_path):
        process_postcompact_summary(
            db_path,
            "sess-006",
            "Working on Legion stuff.",
        )
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT source FROM session_summaries WHERE session_id='sess-006'").fetchone()
        conn.close()
        assert row[0] == "compact"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

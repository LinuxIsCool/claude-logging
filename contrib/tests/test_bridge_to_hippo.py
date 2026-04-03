#!/usr/bin/env python3
"""
Unit tests for bridge_to_hippo.py — Phase 3 entity-to-graph bridging.

Tests entity extraction from SQLite, co-occurrence pair generation,
and the graph query parameter formatting.
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest"]
# ///

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "contrib"))


# ── Schema for test DB ────────────────────────────────────────────────

SCHEMA = """
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
    db = tmp_path / "logging.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    # Insert test entities across multiple sessions
    now = "2026-03-20T00:00:00Z"
    test_data = [
        ("sess-001", "Alice", "Person", 5, now),
        ("sess-001", "Bob", "Person", 3, now),
        ("sess-001", "TestVenture", "Venture", 2, now),
        ("sess-002", "Alice", "Person", 8, now),
        ("sess-002", "Bob", "Person", 4, now),
        ("sess-002", "claude-hippo", "Project", 6, now),
        ("sess-003", "Alice", "Person", 2, now),
        ("sess-003", "TestVenture", "Venture", 3, now),
        ("sess-003", "claude-hippo", "Project", 1, now),
        ("sess-004", "Bob", "Person", 1, now),
        ("sess-004", "Alice", "Person", 1, now),
    ]
    for sid, name, etype, count, ts in test_data:
        conn.execute(
            "INSERT INTO session_entities (session_id, entity_name, entity_type, mention_count, first_seen) VALUES (?,?,?,?,?)",
            (sid, name, etype, count, ts),
        )
    conn.commit()
    conn.close()
    return db


# ── Import after path setup ──────────────────────────────────────────

from bridge_to_hippo import (
    get_session_entities,
    get_session_entity_pairs,
    graph_query,
)


class TestGetSessionEntities:

    def test_min_sessions_filter(self, db_path):
        entities = get_session_entities(db_path, min_sessions=2)
        # Alice appears in 4 sessions, Bob in 3, TestVenture in 2, claude-hippo in 2
        assert "Alice" in entities
        assert "Bob" in entities
        assert "TestVenture" in entities
        assert "claude-hippo" in entities

    def test_high_min_sessions(self, db_path):
        entities = get_session_entities(db_path, min_sessions=4)
        # Only Alice appears in 4 sessions
        assert "Alice" in entities
        assert "Bob" not in entities

    def test_entity_metadata(self, db_path):
        entities = get_session_entities(db_path, min_sessions=2)
        eve = entities["Alice"]
        assert eve["type"] == "Person"
        assert eve["sessions"] == 4
        assert eve["mentions"] == 16  # 5+8+2+1

    def test_empty_db(self, tmp_path):
        db = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(SCHEMA)
        conn.close()
        entities = get_session_entities(db, min_sessions=1)
        assert len(entities) == 0


class TestGetSessionEntityPairs:

    def test_co_occurrence_pairs(self, db_path):
        pairs = get_session_entity_pairs(db_path)
        # Alice and Bob co-occur in sess-001, sess-002, sess-004 = 3 sessions
        pair_dict = {(p[0], p[1]): p[2] for p in pairs}
        # Names sorted alphabetically (a.entity_name < b.entity_name)
        assert ("Alice", "Bob") in pair_dict
        assert pair_dict[("Alice", "Bob")] == 3

    def test_minimum_3_shared_sessions(self, db_path):
        pairs = get_session_entity_pairs(db_path)
        # All pairs should have >= 3 shared sessions
        for _, _, ss in pairs:
            assert ss >= 3


class TestGraphQuery:

    def test_cypher_prefix_format(self):
        """Verify parameter encoding uses CYPHER prefix, not JSON args."""
        mock_redis = MagicMock()
        mock_redis.execute_command = MagicMock(return_value=None)

        graph_query(mock_redis, "MATCH (n) RETURN n", params={
            "name": "Alice", "count": 42,
        })

        call_args = mock_redis.execute_command.call_args
        query_str = call_args[0][2]  # 3rd positional arg is the query
        assert query_str.startswith("CYPHER ")
        assert 'name="Alice"' in query_str
        assert "count=42" in query_str

    def test_no_params(self):
        mock_redis = MagicMock()
        mock_redis.execute_command = MagicMock(return_value=None)

        graph_query(mock_redis, "MATCH (n) RETURN n")

        call_args = mock_redis.execute_command.call_args
        query_str = call_args[0][2]
        assert not query_str.startswith("CYPHER ")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

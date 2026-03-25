#!/usr/bin/env python3
"""
Integration tests for the Memory Roadmap — Phases 1-3.

These tests verify end-to-end flows:
1. Transcript → text extraction → entity extraction → SQLite storage
2. Heartbeat file lifecycle (write → check freshness → detect staleness)
3. Session entity → Hippo bridge (dry-run mode, no FalkorDB required)
4. PostCompact → summary capture → entity storage pipeline

Uses REAL data from the live logging.db and transcript files where available.
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest"]
# ///

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

# Add paths
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "lib"))
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from session_capture import (
    extract_entities_lightweight,
    store_session_summary,
    process_postcompact_summary,
    batch_extract_from_transcripts,
)
from extract_session_text import extract_session, iter_transcripts


# ── Schema ────────────────────────────────────────────────────────────

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
    db = tmp_path / "logging.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.close()
    return db


# ── Integration: Transcript → Entities → SQLite ──────────────────────

class TestTranscriptToEntityPipeline:

    def test_end_to_end_with_fake_transcript(self, tmp_path, db_path):
        """Full pipeline: JSONL → text extraction → NER → SQLite."""
        # Create a realistic transcript
        transcript = tmp_path / "12345678-abcd-1234-abcd-123456789012.jsonl"
        entries = [
            {"type": "user", "message": {"content": "Can you check on the IndigenomicsAI project? The deadline is 2026-04-15."}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "I'll check on IndigenomicsAI status."}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "claude-hippo shows 10,709 nodes"},
                {"type": "text", "text": "What about the Regen AI schedule for tomorrow?"},
            ]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Regen AI has 3 milestones scheduled."}]}},
        ]
        with open(transcript, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

        # Step 1: Extract text
        result = extract_session(transcript)
        assert result is not None
        assert result.user_message_count == 2
        assert result.assistant_message_count == 2
        assert "IndigenomicsAI" in result.full_text
        assert "claude-hippo" in result.full_text

        # Step 2: Extract entities from full text
        entities = extract_entities_lightweight(result.full_text)
        entity_names = {e["name"] for e in entities}
        assert "IndigenomicsAI" in entity_names
        assert "claude-hippo" in entity_names

        # Step 3: Store to SQLite
        session_id = result.session_id
        store_session_summary(
            db_path, session_id, result.user_text, "extracted", entities,
        )

        # Step 4: Verify storage
        conn = sqlite3.connect(str(db_path))
        summary = conn.execute(
            "SELECT summary, source, entities_extracted FROM session_summaries WHERE session_id=?",
            (session_id,)
        ).fetchone()
        assert summary is not None
        assert summary[1] == "extracted"
        assert summary[2] > 0

        stored_entities = conn.execute(
            "SELECT entity_name, entity_type, mention_count FROM session_entities WHERE session_id=?",
            (session_id,)
        ).fetchall()
        conn.close()

        stored_names = {e[0] for e in stored_entities}
        assert "IndigenomicsAI" in stored_names
        assert "claude-hippo" in stored_names

    def test_postcompact_pipeline(self, db_path):
        """PostCompact summary → entity extraction → storage."""
        summary = (
            "Session covered IndigenomicsAI project planning with the team. "
            "Discussed claude-hippo graph indexing, $15,000 budget allocation, "
            "and the 2026-04-12 milestone deadline. Also reviewed Regen AI."
        )
        count = process_postcompact_summary(db_path, "sess-compact-001", summary)

        # Should extract: IndigenomicsAI, Regen AI (Venture),
        # claude-hippo (Project), $15,000 (Money), 2026-04-12 (Date)
        assert count >= 4

        conn = sqlite3.connect(str(db_path))
        entities = conn.execute(
            "SELECT entity_name, entity_type FROM session_entities WHERE session_id='sess-compact-001'"
        ).fetchall()
        conn.close()

        entity_dict = {e[0]: e[1] for e in entities}
        assert entity_dict.get("IndigenomicsAI") == "Venture"
        assert entity_dict.get("claude-hippo") == "Project"


# ── Integration: Heartbeat lifecycle ──────────────────────────────────

class TestHeartbeatLifecycle:

    def test_write_check_cycle(self, tmp_path):
        """Write heartbeat → check → should be fresh."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "log_event",
            str(PLUGIN_ROOT / "hooks" / "log_event.py"),
        )
        log_event = importlib.util.module_from_spec(spec)
        original_argv = sys.argv
        sys.argv = ["log_event.py", "-e", "Stop"]
        spec.loader.exec_module(log_event)
        sys.argv = original_argv

        from unittest.mock import patch
        with patch.object(log_event, "HEALTH_DIR", tmp_path):
            # Write all heartbeats
            for name in ("logging", "embedding", "hippo", "dreams", "rhythm"):
                log_event.write_heartbeat(name)

            # All should be fresh
            stale = log_event.check_stale_heartbeats()
            assert len(stale) == 0

            # Age one heartbeat
            hb = tmp_path / "hippo-heartbeat"
            old = time.time() - 30 * 3600
            os.utime(hb, (old, old))

            # Should detect exactly 1 stale
            stale = log_event.check_stale_heartbeats()
            assert len(stale) == 1
            assert stale[0][0] == "hippo"


# ── Integration: Real data verification ───────────────────────────────

LIVE_DB = Path.home() / ".claude" / "local" / "logging" / "-home-shawn" / "db" / "logging.db"
LIVE_TRANSCRIPTS = Path.home() / ".claude" / "projects" / "-home-shawn"


@pytest.mark.skipif(
    not LIVE_DB.exists(),
    reason="Live logging.db not available"
)
class TestLiveDataVerification:

    def test_session_summaries_populated(self):
        """Verify batch extraction populated session_summaries."""
        conn = sqlite3.connect(str(LIVE_DB), timeout=10)
        try:
            count = conn.execute("SELECT COUNT(*) FROM session_summaries").fetchone()[0]
        except sqlite3.OperationalError:
            pytest.skip("session_summaries table not yet created")
        finally:
            conn.close()
        # After batch extraction, should have significant coverage
        assert count > 0, f"Expected populated session_summaries, got {count}"

    def test_session_entities_populated(self):
        """Verify entity extraction populated session_entities."""
        conn = sqlite3.connect(str(LIVE_DB), timeout=10)
        try:
            count = conn.execute("SELECT COUNT(*) FROM session_entities").fetchone()[0]
        except sqlite3.OperationalError:
            pytest.skip("session_entities table not yet created")
        finally:
            conn.close()
        assert count > 0, f"Expected populated session_entities, got {count}"

    def test_entity_type_distribution(self):
        """Check that multiple entity types are represented."""
        conn = sqlite3.connect(str(LIVE_DB), timeout=10)
        try:
            types = conn.execute(
                "SELECT entity_type, COUNT(*) FROM session_entities GROUP BY entity_type"
            ).fetchall()
        except sqlite3.OperationalError:
            pytest.skip("session_entities table not yet created")
        finally:
            conn.close()
        type_dict = dict(types)
        assert "Person" in type_dict, "Expected Person entities"
        assert "Venture" in type_dict or "Project" in type_dict, "Expected Venture or Project entities"

    def test_top_entities_make_sense(self):
        """Top entities should be known names from the system."""
        conn = sqlite3.connect(str(LIVE_DB), timeout=10)
        try:
            top = conn.execute("""
                SELECT entity_name, SUM(mention_count) as total
                FROM session_entities
                WHERE entity_type = 'Person'
                GROUP BY entity_name
                ORDER BY total DESC
                LIMIT 5
            """).fetchall()
        except sqlite3.OperationalError:
            pytest.skip("session_entities table not yet created")
        finally:
            conn.close()
        names = {t[0] for t in top}
        # Top entities should exist (we can't predict exact names from dynamic DB)
        assert len(names) > 0, f"Expected at least one person entity, got {names}"


@pytest.mark.skipif(
    not LIVE_TRANSCRIPTS.exists(),
    reason="Live transcripts not available"
)
class TestLiveTranscriptVerification:

    def test_can_extract_real_session(self):
        """Pick a real transcript and verify extraction works."""
        transcripts = list(iter_transcripts("/home/shawn"))[:1]
        if not transcripts:
            pytest.skip("No transcripts found")

        result = extract_session(transcripts[0])
        assert result is not None
        assert result.total_chars > 0
        assert result.user_message_count > 0

    def test_transcript_count_matches_expectation(self):
        """Should have 200+ transcripts based on known system state."""
        count = sum(1 for _ in iter_transcripts("/home/shawn"))
        assert count > 100, f"Expected 200+ transcripts, got {count}"


# ── Integration: Heartbeat files exist in real system ─────────────────

HEALTH_DIR = Path.home() / ".claude" / "local" / "health"


@pytest.mark.skipif(
    not HEALTH_DIR.exists(),
    reason="Health directory not yet created"
)
class TestLiveHeartbeats:

    def test_heartbeat_files_exist(self):
        """At least the logging heartbeat should exist after Phase 1."""
        heartbeats = list(HEALTH_DIR.glob("*-heartbeat"))
        assert len(heartbeats) > 0, "No heartbeat files found"

    def test_heartbeat_contents_valid(self):
        """Heartbeat files should contain ISO timestamps."""
        for hb in HEALTH_DIR.glob("*-heartbeat"):
            content = hb.read_text().strip()
            assert "T" in content, f"{hb.name} content not a timestamp: {content}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])

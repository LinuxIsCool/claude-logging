"""Embedding storage tests (blob fallback path — no native sqlite-vec required)."""

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from lib.embeddings import EmbeddingStorage, EmbeddingService


@pytest.fixture
def emb_store(tmp_path):
    store = EmbeddingStorage(tmp_path / "embeddings.db", dimension=4)
    yield store
    store.close()


class TestEmbeddingStorage:
    def test_init_creates_tables(self, emb_store):
        tables = emb_store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {t[0] for t in tables}
        assert "embeddings" in names
        assert "embedding_metadata" in names

    def test_store_and_search(self, emb_store):
        emb_store.store("evt1", [1.0, 0.0, 0.0, 0.0], {
            "session_id": "s1", "event_type": "Stop",
            "content": "hello", "timestamp": "2026-04-01T10:00:00Z",
        })
        results = emb_store.search([1.0, 0.0, 0.0, 0.0], limit=5)
        assert len(results) == 1
        assert results[0]["event_id"] == "evt1"
        assert results[0]["score"] > 0.99

    def test_store_batch(self, emb_store):
        items = [
            {
                "event_id": f"evt{i}",
                "embedding": [float(i == j) for j in range(4)],
                "metadata": {
                    "session_id": "s1", "event_type": "Stop",
                    "content": f"text{i}", "timestamp": "2026-04-01T10:00:00Z",
                },
            }
            for i in range(4)
        ]
        count = emb_store.store_batch(items)
        assert count == 4

    def test_search_with_event_type_filter(self, emb_store):
        emb_store.store("evt1", [1.0, 0.0, 0.0, 0.0], {
            "session_id": "s1", "event_type": "Stop",
            "content": "a", "timestamp": "t",
        })
        emb_store.store("evt2", [0.9, 0.1, 0.0, 0.0], {
            "session_id": "s1", "event_type": "UserPromptSubmit",
            "content": "b", "timestamp": "t",
        })
        results = emb_store.search(
            [1.0, 0.0, 0.0, 0.0], limit=5,
            filters={"event_types": ["UserPromptSubmit"]},
        )
        assert all(r["event_type"] == "UserPromptSubmit" for r in results)

    def test_search_empty_db(self, emb_store):
        results = emb_store.search([1.0, 0.0, 0.0, 0.0], limit=5)
        assert results == []

    def test_serialize_deserialize_roundtrip(self, emb_store):
        original = [1.0, -0.5, 0.333, 1e-6]
        blob = emb_store._serialize_embedding(original)
        recovered = emb_store._deserialize_embedding(blob)
        for a, b in zip(original, recovered):
            assert abs(a - b) < 1e-6


class TestEmbeddingServiceGraceful:
    def test_unavailable_returns_empty(self):
        svc = EmbeddingService.__new__(EmbeddingService)
        svc.model = None
        svc.dimension = 384
        assert svc.is_available is False
        assert svc.encode(["hello"]) == []
        assert svc.encode_single("hello") is None

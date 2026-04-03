"""Search layer tests: FTS5, RRF fusion, hybrid search."""

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from lib.search import SearchResult, _sanitize_fts_query


class TestFTSSanitization:
    def test_plain_text_unchanged_in_spirit(self):
        result = _sanitize_fts_query("hello world")
        assert "hello world" in result

    def test_quotes_escaped(self):
        result = _sanitize_fts_query('say "hello"')
        # Internal quotes doubled, outer quotes wrap
        assert '""hello""' in result

    def test_fts_operators_neutralized(self):
        result = _sanitize_fts_query("session_id:evil*")
        # Wrapped in quotes, so FTS5 treats as literal
        assert result.startswith('"')
        assert result.endswith('"')


class TestKeywordSearch:
    def test_returns_results(self, search_service):
        results = search_service.keyword_search("authentication", limit=10)
        assert len(results) >= 1
        assert all(r.source == "keyword" for r in results)
        assert all(r.score > 0 for r in results)

    def test_empty_query(self, search_service):
        results = search_service.keyword_search("", limit=10)
        assert results == []

    def test_whitespace_query(self, search_service):
        results = search_service.keyword_search("   ", limit=10)
        assert results == []

    def test_event_type_filter(self, search_service):
        results = search_service.keyword_search(
            "auth", event_types=["UserPromptSubmit"],
        )
        assert all(r.event_type == "UserPromptSubmit" for r in results)

    def test_date_range_filter(self, search_service):
        results = search_service.keyword_search(
            "database", date_from="2026-04-02", date_to="2026-04-02T23:59:59",
        )
        for r in results:
            assert "2026-04-02" in r.timestamp

    def test_bm25_scores_positive(self, search_service):
        results = search_service.keyword_search("authentication")
        for r in results:
            assert r.score > 0, f"Score should be positive, got {r.score}"


class TestReciprocalRankFusion:
    def test_overlap_boosts(self, search_service):
        kw = [
            SearchResult("e1", "s1", "Stop", "hello", 1.0, "2026-04-01T10:00:00Z", "keyword"),
            SearchResult("e2", "s1", "Stop", "world", 0.5, "2026-04-01T10:00:00Z", "keyword"),
        ]
        sem = [
            SearchResult("e2", "s1", "Stop", "world", 0.9, "2026-04-01T10:00:00Z", "semantic"),
            SearchResult("e3", "s1", "Stop", "test", 0.3, "2026-04-01T10:00:00Z", "semantic"),
        ]
        fused = search_service.reciprocal_rank_fusion(kw, sem)
        ids = [r.event_id for r in fused]
        # e2 appears in both — should rank highest
        assert ids[0] == "e2"
        assert len(fused) == 3

    def test_disjoint_sets(self, search_service):
        kw = [SearchResult("e1", "s1", "Stop", "a", 1.0, "t", "keyword")]
        sem = [SearchResult("e2", "s1", "Stop", "b", 1.0, "t", "semantic")]
        fused = search_service.reciprocal_rank_fusion(kw, sem)
        assert len(fused) == 2

    def test_empty_inputs(self, search_service):
        fused = search_service.reciprocal_rank_fusion([], [])
        assert fused == []

    def test_all_hybrid_source(self, search_service):
        kw = [SearchResult("e1", "s1", "Stop", "a", 1.0, "t", "keyword")]
        sem = [SearchResult("e1", "s1", "Stop", "a", 0.8, "t", "semantic")]
        fused = search_service.reciprocal_rank_fusion(kw, sem)
        assert all(r.source == "hybrid" for r in fused)


class TestHybridSearch:
    def test_keyword_only_when_no_semantic(self, search_service):
        assert search_service.embedding_service is None
        results, ms = search_service.hybrid_search("authentication")
        assert len(results) >= 1
        assert ms >= 0

    def test_returns_timing(self, search_service):
        results, ms = search_service.hybrid_search("test")
        assert isinstance(ms, float)
        assert ms >= 0

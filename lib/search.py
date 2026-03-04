"""
Hybrid Search Service for Logging Plugin

Combines FTS5 keyword search with optional semantic search,
using Reciprocal Rank Fusion (RRF) to merge results.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple
from pathlib import Path
import time

from .storage import SQLiteStorage


@dataclass
class SearchResult:
    """Search result with scoring."""
    event_id: str
    session_id: str
    event_type: str
    content: str
    score: float  # RRF score for ranking (0.01-0.03 typical)
    timestamp: str
    source: str = "keyword"  # 'keyword', 'semantic', or 'hybrid'
    cosine_similarity: float = 0.0  # Raw semantic similarity (0.0-1.0) for display


class SearchService:
    """Hybrid search service combining keyword and semantic search."""

    def __init__(self, sqlite: SQLiteStorage, embedding_service=None):
        self.sqlite = sqlite
        self.embeddings = embedding_service

    def keyword_search(
        self,
        query: str,
        limit: int = 20,
        event_types: Optional[List[str]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None
    ) -> List[SearchResult]:
        """FTS5 keyword search with BM25 ranking."""
        # Build query with filters
        sql = """
            SELECT
                e.id,
                e.session_id,
                e.type,
                e.ts,
                e.content,
                bm25(events_fts) as score
            FROM events_fts
            JOIN events e ON events_fts.event_id = e.id
            WHERE events_fts MATCH ?
        """
        params = [query]

        if event_types:
            placeholders = ",".join("?" * len(event_types))
            sql += f" AND e.type IN ({placeholders})"
            params.extend(event_types)

        if date_from:
            sql += " AND e.ts >= ?"
            params.append(date_from)

        if date_to:
            sql += " AND e.ts <= ?"
            params.append(date_to)

        sql += " ORDER BY score LIMIT ?"
        params.append(limit)

        cursor = self.sqlite.conn.execute(sql, params)

        results = []
        for row in cursor:
            results.append(SearchResult(
                event_id=row[0],
                session_id=row[1],
                event_type=row[2],
                timestamp=row[3],
                content=row[4] or "",
                score=abs(row[5]),  # BM25 returns negative scores
                source="keyword"
            ))

        return results

    def semantic_search(
        self,
        query: str,
        limit: int = 20,
        event_types: Optional[List[str]] = None
    ) -> List[SearchResult]:
        """
        Semantic search using embeddings.
        Falls back to empty results if embeddings not available.
        """
        if self.embeddings is None:
            return []

        # Get query embedding
        query_embedding = self.embeddings.encode([query])[0]

        # Search similar embeddings
        # This would use sqlite-vec in production
        results = self.embeddings.search(
            query_embedding,
            limit=limit,
            filters={"event_types": event_types} if event_types else None
        )

        return [
            SearchResult(
                event_id=r["event_id"],
                session_id=r["session_id"],
                event_type=r["event_type"],
                content=r["content"],
                score=r["score"],  # This is cosine similarity from embeddings
                timestamp=r["timestamp"],
                source="semantic",
                cosine_similarity=r["score"]  # Preserve raw cosine for display
            )
            for r in results
        ]

    def reciprocal_rank_fusion(
        self,
        keyword_results: List[SearchResult],
        semantic_results: List[SearchResult],
        k: int = 60
    ) -> List[SearchResult]:
        """
        Combine results using Reciprocal Rank Fusion.

        RRF score = Σ 1/(k + rank)

        This is rank-based (not score-based), making it robust to
        different score distributions from different search methods.

        Preserves cosine_similarity from semantic results for display purposes.
        """
        scores = {}
        result_map = {}
        cosine_scores = {}  # Preserve cosine similarity from semantic search

        # Score from keyword results
        for rank, result in enumerate(keyword_results):
            rrf_score = 1 / (k + rank + 1)
            scores[result.event_id] = scores.get(result.event_id, 0) + rrf_score
            result_map[result.event_id] = result

        # Score from semantic results
        for rank, result in enumerate(semantic_results):
            rrf_score = 1 / (k + rank + 1)
            scores[result.event_id] = scores.get(result.event_id, 0) + rrf_score
            # Preserve cosine similarity from semantic results
            cosine_scores[result.event_id] = result.cosine_similarity
            if result.event_id not in result_map:
                result_map[result.event_id] = result

        # Sort by combined RRF score
        sorted_ids = sorted(scores.keys(), key=lambda x: -scores[x])

        results = []
        for event_id in sorted_ids:
            result = result_map[event_id]
            results.append(SearchResult(
                event_id=result.event_id,
                session_id=result.session_id,
                event_type=result.event_type,
                content=result.content,
                score=scores[event_id],
                timestamp=result.timestamp,
                source="hybrid",
                cosine_similarity=cosine_scores.get(event_id, 0.0)
            ))

        return results

    def hybrid_search(
        self,
        query: str,
        limit: int = 20,
        event_types: Optional[List[str]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        use_semantic: bool = True
    ) -> Tuple[List[SearchResult], float]:
        """
        Perform hybrid search combining keyword and semantic results.
        Returns (results, time_ms).
        """
        start_time = time.perf_counter()

        # Get keyword results
        keyword_results = self.keyword_search(
            query,
            limit=limit * 2,  # Get more for fusion
            event_types=event_types,
            date_from=date_from,
            date_to=date_to
        )

        # Get semantic results if enabled and available
        semantic_results = []
        if use_semantic and self.embeddings is not None:
            semantic_results = self.semantic_search(
                query,
                limit=limit * 2,
                event_types=event_types
            )

        # Fuse results
        if semantic_results:
            results = self.reciprocal_rank_fusion(
                keyword_results, semantic_results
            )
        else:
            results = keyword_results

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        return results[:limit], elapsed_ms

    def get_suggestions(self, prefix: str, limit: int = 10) -> List[str]:
        """Get search suggestions based on prefix."""
        # Simple implementation: find matching content prefixes
        cursor = self.sqlite.conn.execute("""
            SELECT DISTINCT content
            FROM events
            WHERE content LIKE ? || '%'
            LIMIT ?
        """, (prefix, limit))

        return [row[0] for row in cursor if row[0]]

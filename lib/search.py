"""
Hybrid Search Service for Logging Plugin

Combines FTS5 keyword search with optional semantic search,
using Reciprocal Rank Fusion (RRF) to merge results.
"""

import sqlite3
import time
from dataclasses import dataclass

from .storage import SQLiteStorage


def _sanitize_fts_query(query: str) -> str:
    """Escape user input for FTS5 MATCH to prevent syntax errors.

    Wraps the query in double-quotes so FTS5 treats it as a literal phrase.
    Internal double-quotes are escaped per FTS5 convention.
    """
    return '"' + query.replace('"', '""') + '"'


@dataclass
class SearchResult:
    """Search result with scoring."""

    event_id: str
    session_id: str
    event_type: str
    content: str
    score: float
    timestamp: str
    source: str = "keyword"  # 'keyword', 'semantic', or 'hybrid'


class SearchService:
    """Hybrid search service combining keyword and semantic search."""

    def __init__(self, sqlite: SQLiteStorage, embedding_service=None, embedding_storage=None):
        self.sqlite = sqlite
        self.embedding_service = embedding_service
        self.embedding_storage = embedding_storage
        # Backward compat: if a single object has both encode+search, use it
        self.embeddings = embedding_service

    def keyword_search(
        self,
        query: str,
        limit: int = 20,
        event_types: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[SearchResult]:
        """FTS5 keyword search with BM25 ranking."""
        if not query or not query.strip():
            return []

        # Sanitize for FTS5 syntax safety
        safe_query = _sanitize_fts_query(query.strip())

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
        params: list = [safe_query]

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

        try:
            cursor = self.sqlite.conn.execute(sql, params)
        except sqlite3.OperationalError:
            return []  # Malformed query — return empty rather than 500

        results = []
        for row in cursor:
            results.append(
                SearchResult(
                    event_id=row[0],
                    session_id=row[1],
                    event_type=row[2],
                    timestamp=row[3],
                    content=row[4] or "",
                    score=abs(row[5]),  # BM25 returns negative scores
                    source="keyword",
                )
            )

        return results

    def semantic_search(
        self,
        query: str,
        limit: int = 20,
        event_types: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[SearchResult]:
        """
        Semantic search using embeddings.
        Falls back to empty results if embeddings not available.
        """
        # Build unified filters dict
        filters = {}
        if event_types:
            filters["event_types"] = event_types
        if date_from:
            filters["date_from"] = date_from
        if date_to:
            filters["date_to"] = date_to

        search_filters = filters if filters else None

        # Need both an encoder and a storage backend
        if self.embedding_service is None or self.embedding_storage is None:
            # Backward compat: single object with encode+search
            if self.embeddings is not None and hasattr(self.embeddings, "search"):
                query_embedding = self.embeddings.encode([query])[0]
                results = self.embeddings.search(query_embedding, limit=limit, filters=search_filters)
            else:
                return []
        else:
            # Standard path: separate encoder + storage
            query_embedding = self.embedding_service.encode([query])[0]
            results = self.embedding_storage.search(query_embedding, limit=limit, filters=search_filters)

        return [
            SearchResult(
                event_id=r["event_id"],
                session_id=r["session_id"],
                event_type=r["event_type"],
                content=r["content"],
                score=r["score"],
                timestamp=r["timestamp"],
                source="semantic",
            )
            for r in results
        ]

    def reciprocal_rank_fusion(
        self, keyword_results: list[SearchResult], semantic_results: list[SearchResult], k: int = 60
    ) -> list[SearchResult]:
        """
        Combine results using Reciprocal Rank Fusion.

        RRF score = Σ 1/(k + rank)

        This is rank-based (not score-based), making it robust to
        different score distributions from different search methods.
        """
        scores = {}
        result_map = {}

        # Score from keyword results
        for rank, result in enumerate(keyword_results):
            rrf_score = 1 / (k + rank + 1)
            scores[result.event_id] = scores.get(result.event_id, 0) + rrf_score
            result_map[result.event_id] = result

        # Score from semantic results
        for rank, result in enumerate(semantic_results):
            rrf_score = 1 / (k + rank + 1)
            scores[result.event_id] = scores.get(result.event_id, 0) + rrf_score
            if result.event_id not in result_map:
                result_map[result.event_id] = result

        # Sort by combined RRF score
        sorted_ids = sorted(scores.keys(), key=lambda x: -scores[x])

        results = []
        for event_id in sorted_ids:
            result = result_map[event_id]
            results.append(
                SearchResult(
                    event_id=result.event_id,
                    session_id=result.session_id,
                    event_type=result.event_type,
                    content=result.content,
                    score=scores[event_id],
                    timestamp=result.timestamp,
                    source="hybrid",
                )
            )

        return results

    def hybrid_search(
        self,
        query: str,
        limit: int = 20,
        event_types: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        use_semantic: bool = True,
    ) -> tuple[list[SearchResult], float]:
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
            date_to=date_to,
        )

        # Get semantic results if enabled and available
        semantic_results = []
        if use_semantic and self.embeddings is not None:
            semantic_results = self.semantic_search(
                query, limit=limit * 2, event_types=event_types, date_from=date_from, date_to=date_to
            )

        # Fuse results
        if semantic_results:
            results = self.reciprocal_rank_fusion(keyword_results, semantic_results)
        else:
            results = keyword_results

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        return results[:limit], elapsed_ms

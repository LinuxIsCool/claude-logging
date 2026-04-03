"""
Embedding Service for Logging Plugin

Provides local embedding generation using sentence-transformers.
Falls back gracefully when not installed.
"""

import struct
from pathlib import Path
from typing import Any


class EmbeddingService:
    """
    Local embedding service using sentence-transformers.

    Recommended model: all-MiniLM-L6-v2
    - 384 dimensions
    - 22MB model size
    - Fast inference (~5000 sentences/sec on CPU)
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model_name = model_name
        self.model = None
        self.dimension = 384  # all-MiniLM-L6-v2 dimension
        self._load_model()

    def _load_model(self) -> bool:
        """Attempt to load the embedding model."""
        try:
            from sentence_transformers import SentenceTransformer

            self.model = SentenceTransformer(self.model_name)
            self.dimension = self.model.get_sentence_embedding_dimension()
            return True
        except ImportError:
            # sentence-transformers not installed
            return False
        except Exception:
            # Model loading failed
            return False

    @property
    def is_available(self) -> bool:
        """Check if embeddings are available."""
        return self.model is not None

    def encode(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for a list of texts.

        Returns empty list if model not available.
        """
        if not self.is_available:
            return []

        embeddings = self.model.encode(texts, convert_to_numpy=True)
        return embeddings.tolist()

    def encode_single(self, text: str) -> list[float] | None:
        """Generate embedding for a single text."""
        if not self.is_available:
            return None

        result = self.encode([text])
        return result[0] if result else None

    def similarity(self, embedding1: list[float], embedding2: list[float]) -> float:
        """Calculate cosine similarity between two embeddings."""
        try:
            import numpy as np

            a = np.array(embedding1)
            b = np.array(embedding2)

            return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
        except ImportError:
            # Fallback: manual calculation
            dot_product = sum(a * b for a, b in zip(embedding1, embedding2))
            norm1 = sum(a * a for a in embedding1) ** 0.5
            norm2 = sum(b * b for b in embedding2) ** 0.5
            return dot_product / (norm1 * norm2) if norm1 and norm2 else 0.0


class EmbeddingStorage:
    """
    Storage for embeddings using sqlite-vec or file-based fallback.

    Uses binary format for efficient storage:
    - 4 bytes per float32
    - 384 dimensions = 1.5KB per embedding
    """

    def __init__(self, db_path: Path, dimension: int = 384):
        self.db_path = db_path
        self.dimension = dimension
        self.conn = None
        self._init_storage()

    def _init_storage(self):
        """Initialize embedding storage."""
        import sqlite3

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))

        # Try to load sqlite-vec extension
        try:
            self.conn.enable_load_extension(True)
            self.conn.load_extension("vec0")
            self._has_vec = True
        except Exception:
            self._has_vec = False

        # Create tables
        if self._has_vec:
            self.conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS embeddings USING vec0(
                    event_id TEXT PRIMARY KEY,
                    embedding FLOAT[{self.dimension}]
                )
            """)
        else:
            # Fallback: store as blob
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS embeddings (
                    event_id TEXT PRIMARY KEY,
                    embedding BLOB NOT NULL
                )
            """)

        # Metadata table
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS embedding_metadata (
                event_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                content TEXT,
                timestamp TEXT
            )
        """)

        # Indexes for filtered search
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_emb_meta_event_type ON embedding_metadata(event_type)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_emb_meta_timestamp ON embedding_metadata(timestamp)")

        self.conn.commit()

    def _serialize_embedding(self, embedding: list[float]) -> bytes:
        """Serialize embedding to bytes."""
        return struct.pack(f"{len(embedding)}f", *embedding)

    def _deserialize_embedding(self, data: bytes) -> list[float]:
        """Deserialize embedding from bytes."""
        count = len(data) // 4
        return list(struct.unpack(f"{count}f", data))

    def store(self, event_id: str, embedding: list[float], metadata: dict[str, Any]) -> None:
        """Store an embedding with metadata."""
        blob = self._serialize_embedding(embedding)
        self.conn.execute("INSERT OR REPLACE INTO embeddings (event_id, embedding) VALUES (?, ?)", (event_id, blob))

        self.conn.execute(
            """
            INSERT OR REPLACE INTO embedding_metadata
            (event_id, session_id, event_type, content, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """,
            (
                event_id,
                metadata.get("session_id", ""),
                metadata.get("event_type", ""),
                metadata.get("content", ""),
                metadata.get("timestamp", ""),
            ),
        )

        self.conn.commit()

    def _build_filter_ids(self, filters: dict[str, Any] | None) -> set | None:
        """Build set of event_ids matching filters, or None if no filters."""
        if not filters:
            return None
        has_filter = filters.get("event_types") or filters.get("date_from") or filters.get("date_to")
        if not has_filter:
            return None

        conditions, params = ["1=1"], []
        if filters.get("event_types"):
            placeholders = ",".join("?" * len(filters["event_types"]))
            conditions.append(f"event_type IN ({placeholders})")
            params.extend(filters["event_types"])
        if filters.get("date_from"):
            conditions.append("timestamp >= ?")
            params.append(filters["date_from"])
        if filters.get("date_to"):
            conditions.append("timestamp <= ?")
            params.append(filters["date_to"])

        cursor = self.conn.execute(f"SELECT event_id FROM embedding_metadata WHERE {' AND '.join(conditions)}", params)
        return {row[0] for row in cursor}

    def search(
        self, query_embedding: list[float], limit: int = 20, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """
        Search for similar embeddings.

        Returns list of dicts with event_id, score, and metadata.
        Supports filters: event_types (list), date_from (str), date_to (str).
        """
        filter_ids = self._build_filter_ids(filters)
        if filter_ids is not None and not filter_ids:
            return []  # Filters matched nothing

        if self._has_vec:
            # sqlite-vec MATCH doesn't support arbitrary WHERE on joins,
            # so over-fetch 3x and post-filter
            fetch_limit = limit * 3 if filter_ids is not None else limit
            cursor = self.conn.execute(
                """
                SELECT
                    e.event_id,
                    distance,
                    m.session_id,
                    m.event_type,
                    m.content,
                    m.timestamp
                FROM embeddings e
                JOIN embedding_metadata m ON e.event_id = m.event_id
                WHERE embedding MATCH ?
                ORDER BY distance
                LIMIT ?
            """,
                (query_embedding, fetch_limit),
            )

            results = []
            for row in cursor:
                if filter_ids is not None and row[0] not in filter_ids:
                    continue
                results.append(
                    {
                        "event_id": row[0],
                        "score": 1 - row[1],  # Convert distance to similarity
                        "session_id": row[2],
                        "event_type": row[3],
                        "content": row[4],
                        "timestamp": row[5],
                    }
                )
                if len(results) >= limit:
                    break
            return results
        else:
            # Fallback: vectorized brute-force search (numpy)
            try:
                import numpy as np
            except ImportError:
                return []

            cursor = self.conn.execute("SELECT event_id, embedding FROM embeddings")
            rows = cursor.fetchall()
            if not rows:
                return []

            # Pre-filter by metadata before deserializing embeddings
            if filter_ids is not None:
                rows = [r for r in rows if r[0] in filter_ids]
                if not rows:
                    return []

            all_ids = [r[0] for r in rows]
            all_embeddings = np.array([self._deserialize_embedding(r[1]) for r in rows])
            query_np = np.array(query_embedding)

            # Vectorized cosine similarity
            norms = np.linalg.norm(all_embeddings, axis=1) * np.linalg.norm(query_np)
            norms[norms == 0] = 1  # avoid division by zero
            scores = all_embeddings @ query_np / norms

            # Top-k by score
            top_indices = np.argsort(-scores)[:limit]
            top_ids = [all_ids[i] for i in top_indices]
            top_scores = [float(scores[i]) for i in top_indices]

            # Fetch metadata for top results
            final_results = []
            for event_id, score in zip(top_ids, top_scores):
                meta_cursor = self.conn.execute(
                    """
                    SELECT session_id, event_type, content, timestamp
                    FROM embedding_metadata WHERE event_id = ?
                """,
                    (event_id,),
                )
                meta = meta_cursor.fetchone()
                if meta:
                    final_results.append(
                        {
                            "event_id": event_id,
                            "score": score,
                            "session_id": meta[0],
                            "event_type": meta[1],
                            "content": meta[2],
                            "timestamp": meta[3],
                        }
                    )

            return final_results

    def store_batch(self, items: list[dict[str, Any]]) -> int:
        """Store multiple embeddings in a single transaction.

        Each item must have: event_id, embedding, metadata (dict with
        session_id, event_type, content, timestamp).
        Returns count of items stored.
        """
        count = 0
        try:
            for item in items:
                event_id = item["event_id"]
                embedding = item["embedding"]
                metadata = item["metadata"]

                blob = self._serialize_embedding(embedding)
                self.conn.execute(
                    "INSERT OR REPLACE INTO embeddings (event_id, embedding) VALUES (?, ?)", (event_id, blob)
                )

                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO embedding_metadata
                    (event_id, session_id, event_type, content, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                """,
                    (
                        event_id,
                        metadata.get("session_id", ""),
                        metadata.get("event_type", ""),
                        metadata.get("content", ""),
                        metadata.get("timestamp", ""),
                    ),
                )
                count += 1

            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return count

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()

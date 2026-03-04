"""
Embedding Service for Logging Plugin

Provides local embedding generation using sentence-transformers.
Falls back gracefully when not installed.
"""

from typing import List, Optional, Dict, Any
from pathlib import Path
import json
import struct


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

    def encode(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embeddings for a list of texts.

        Returns empty list if model not available.
        """
        if not self.is_available:
            return []

        embeddings = self.model.encode(texts, convert_to_numpy=True)
        return embeddings.tolist()

    def encode_single(self, text: str) -> Optional[List[float]]:
        """Generate embedding for a single text."""
        if not self.is_available:
            return None

        result = self.encode([text])
        return result[0] if result else None

    def similarity(self, embedding1: List[float], embedding2: List[float]) -> float:
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

        self.conn.commit()

    def _serialize_embedding(self, embedding: List[float]) -> bytes:
        """Serialize embedding to bytes."""
        return struct.pack(f'{len(embedding)}f', *embedding)

    def _deserialize_embedding(self, data: bytes) -> List[float]:
        """Deserialize embedding from bytes."""
        count = len(data) // 4
        return list(struct.unpack(f'{count}f', data))

    def store(
        self,
        event_id: str,
        embedding: List[float],
        metadata: Dict[str, Any]
    ) -> None:
        """Store an embedding with metadata."""
        if self._has_vec:
            self.conn.execute(
                "INSERT OR REPLACE INTO embeddings (event_id, embedding) VALUES (?, ?)",
                (event_id, embedding)
            )
        else:
            blob = self._serialize_embedding(embedding)
            self.conn.execute(
                "INSERT OR REPLACE INTO embeddings (event_id, embedding) VALUES (?, ?)",
                (event_id, blob)
            )

        self.conn.execute("""
            INSERT OR REPLACE INTO embedding_metadata
            (event_id, session_id, event_type, content, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """, (
            event_id,
            metadata.get("session_id", ""),
            metadata.get("event_type", ""),
            metadata.get("content", ""),
            metadata.get("timestamp", ""),
        ))

        self.conn.commit()

    def search(
        self,
        query_embedding: List[float],
        limit: int = 20,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Search for similar embeddings.

        Returns list of dicts with event_id, score, and metadata.
        """
        if self._has_vec:
            # Use sqlite-vec for fast vector search
            cursor = self.conn.execute(f"""
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
            """, (query_embedding, limit))
        else:
            # Fallback: brute-force search
            cursor = self.conn.execute("""
                SELECT event_id, embedding FROM embeddings
            """)

            results = []
            for row in cursor:
                event_id = row[0]
                embedding = self._deserialize_embedding(row[1])

                # Calculate cosine similarity
                dot_product = sum(a * b for a, b in zip(query_embedding, embedding))
                norm1 = sum(a * a for a in query_embedding) ** 0.5
                norm2 = sum(b * b for b in embedding) ** 0.5
                score = dot_product / (norm1 * norm2) if norm1 and norm2 else 0.0

                results.append((event_id, score))

            # Sort by similarity (descending)
            results.sort(key=lambda x: -x[1])
            results = results[:limit]

            # Fetch metadata for top results
            final_results = []
            for event_id, score in results:
                meta_cursor = self.conn.execute("""
                    SELECT session_id, event_type, content, timestamp
                    FROM embedding_metadata
                    WHERE event_id = ?
                """, (event_id,))
                meta = meta_cursor.fetchone()
                if meta:
                    final_results.append({
                        "event_id": event_id,
                        "score": score,
                        "session_id": meta[0],
                        "event_type": meta[1],
                        "content": meta[2],
                        "timestamp": meta[3],
                    })

            return final_results

        return [
            {
                "event_id": row[0],
                "score": 1 - row[1],  # Convert distance to similarity
                "session_id": row[2],
                "event_type": row[3],
                "content": row[4],
                "timestamp": row[5],
            }
            for row in cursor
        ]

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()

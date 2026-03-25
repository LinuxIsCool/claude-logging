#!/usr/bin/env python3
"""
Backfill embeddings for all logged events with searchable content.

Embeds high-value event types (prompts, responses, subagent stops) into
EmbeddingStorage for semantic search. Runs incrementally — skips already
embedded events.

Usage:
    cd ~/.claude/plugins/local/legion-plugins/plugins/claude-logging
    uv run scripts/embed_backfill.py [--project-path $HOME] [--all-types] [--batch-size 256]
"""

import sys
import time
import argparse
from pathlib import Path

# Add plugin root to path
plugin_root = str(Path(__file__).resolve().parent.parent)
if plugin_root not in sys.path:
    sys.path.insert(0, plugin_root)

from lib import encode_project_path
from lib.embeddings import EmbeddingService, EmbeddingStorage
from lib.storage import SQLiteStorage

# High-value event types for semantic search
# Tool events are noisy — prompts/responses carry meaning
HIGH_VALUE_TYPES = (
    "UserPromptSubmit",
    "AssistantResponse",
    "Stop",
    "SubagentStop",
    "Notification",
    "SessionStart",
)

def _get_storage_base(project_path: str) -> Path:
    """Compute storage base from project path."""
    return Path.home() / ".claude" / "local" / "logging" / encode_project_path(project_path)


def backfill(project_path: str = "", all_types: bool = False, batch_size: int = 256):
    if not project_path:
        project_path = str(Path.home())
    storage_base = _get_storage_base(project_path)
    db_path = storage_base / "db" / "logging.db"
    emb_db_path = storage_base / "db" / "embeddings.db"

    print(f"SQLite DB: {db_path}")
    print(f"Embeddings DB: {emb_db_path}")

    # Load services
    print("Loading embedding model...")
    t0 = time.perf_counter()
    emb_service = EmbeddingService()
    if not emb_service.is_available:
        print("ERROR: Embedding model not available. Install sentence-transformers.")
        sys.exit(1)
    print(f"  Model loaded in {time.perf_counter() - t0:.1f}s")

    emb_storage = EmbeddingStorage(emb_db_path, dimension=emb_service.dimension)
    sqlite = SQLiteStorage(db_path)

    # Find events needing embedding
    type_filter = ""
    if not all_types:
        placeholders = ",".join(f"'{t}'" for t in HIGH_VALUE_TYPES)
        type_filter = f"AND e.type IN ({placeholders})"

    # Get already-embedded event IDs
    existing = set()
    try:
        cursor = emb_storage.conn.execute("SELECT event_id FROM embedding_metadata")
        existing = {row[0] for row in cursor}
    except Exception:
        pass

    print(f"Already embedded: {len(existing)} events")

    # Fetch events with content that haven't been embedded
    cursor = sqlite.conn.execute(f"""
        SELECT e.id, e.session_id, e.type, e.ts, e.content
        FROM events e
        WHERE e.content IS NOT NULL AND e.content != ''
        {type_filter}
        ORDER BY e.ts
    """)

    # Batch process
    batch_texts = []
    batch_meta = []
    total_embedded = 0
    total_skipped = 0

    t_start = time.perf_counter()

    for row in cursor:
        event_id, session_id, event_type, ts, content = row

        if event_id in existing:
            total_skipped += 1
            continue

        batch_texts.append(content)
        batch_meta.append({
            "event_id": event_id,
            "session_id": session_id,
            "event_type": event_type,
            "content": content,
            "timestamp": ts,
        })

        if len(batch_texts) >= batch_size:
            embeddings = emb_service.encode(batch_texts)
            batch_items = [
                {"event_id": m["event_id"], "embedding": e, "metadata": m}
                for e, m in zip(embeddings, batch_meta)
            ]
            emb_storage.store_batch(batch_items)
            total_embedded += len(batch_texts)
            elapsed = time.perf_counter() - t_start
            rate = total_embedded / elapsed if elapsed > 0 else 0
            print(f"  Embedded {total_embedded} events ({rate:.0f}/sec)...")
            batch_texts = []
            batch_meta = []

    # Final batch
    if batch_texts:
        embeddings = emb_service.encode(batch_texts)
        batch_items = [
            {"event_id": m["event_id"], "embedding": e, "metadata": m}
            for e, m in zip(embeddings, batch_meta)
        ]
        emb_storage.store_batch(batch_items)
        total_embedded += len(batch_texts)

    elapsed = time.perf_counter() - t_start
    rate = total_embedded / elapsed if elapsed > 0 else 0

    print(f"\nDone: {total_embedded} embedded, {total_skipped} skipped, {elapsed:.1f}s ({rate:.0f}/sec)")
    print(f"Embeddings DB size: {emb_db_path.stat().st_size / 1024 / 1024:.1f}MB")

    emb_storage.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill event embeddings")
    parser.add_argument("--project-path", default=str(Path.home()),
                        help="Project directory (default: $HOME)")
    parser.add_argument("--all-types", action="store_true", help="Embed all event types, not just high-value")
    parser.add_argument("--batch-size", type=int, default=256, help="Batch size for encoding")
    args = parser.parse_args()
    backfill(project_path=args.project_path, all_types=args.all_types, batch_size=args.batch_size)

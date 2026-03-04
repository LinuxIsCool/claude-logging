#!/usr/bin/env python3
"""
Backfill Embeddings for Existing Events

Generates embeddings for events that don't have them yet.
Uses batched processing to avoid memory issues with large datasets.
"""

import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import sqlite3
from lib.embeddings import EmbeddingService, EmbeddingStorage
import os


def get_storage_path() -> Path:
    """Get the storage path from environment or default."""
    return Path(os.environ.get(
        "LOGGING_STORAGE_PATH",
        os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()), ".claude/local/logging")
    ))


def backfill_embeddings(batch_size: int = 100, dry_run: bool = False):
    """
    Backfill embeddings for events that don't have them.

    Args:
        batch_size: Number of events to process at once
        dry_run: If True, just count events without generating embeddings
    """
    storage_path = get_storage_path()

    # Initialize services
    print("Initializing embedding service...")
    embedding_service = EmbeddingService()

    if not embedding_service.is_available:
        print("ERROR: sentence-transformers not installed")
        print("Install with: pip install sentence-transformers")
        return 1

    print(f"✓ Model loaded: {embedding_service.model_name}")
    print(f"✓ Dimension: {embedding_service.dimension}")

    # Connect to databases
    events_db = storage_path / "db" / "logging.db"
    if not events_db.exists():
        print(f"ERROR: Events database not found at {events_db}")
        return 1

    events_conn = sqlite3.connect(str(events_db))
    embedding_storage = EmbeddingStorage(storage_path / "embeddings.db")

    # Get set of event IDs that already have embeddings
    existing_embeddings = set()
    try:
        cursor = embedding_storage.conn.execute("SELECT event_id FROM embedding_metadata")
        existing_embeddings = {row[0] for row in cursor}
    except Exception:
        pass  # Table might not exist yet

    # Count total events with content
    cursor = events_conn.execute("SELECT COUNT(*) FROM events WHERE content IS NOT NULL AND content != ''")
    total_count = cursor.fetchone()[0]

    # Get count of events that need embeddings
    cursor = events_conn.execute("SELECT id FROM events WHERE content IS NOT NULL AND content != ''")
    all_event_ids = {row[0] for row in cursor}
    missing_ids = all_event_ids - existing_embeddings
    missing_count = len(missing_ids)

    print(f"\nEvents with content: {total_count}")
    print(f"Already have embeddings: {len(existing_embeddings)}")
    print(f"Missing embeddings: {missing_count}")

    if dry_run:
        print(f"\n[DRY RUN] Would generate embeddings for {missing_count} events")
        events_conn.close()
        embedding_storage.close()
        return 0

    if missing_count == 0:
        print("\n✓ All events already have embeddings!")
        events_conn.close()
        embedding_storage.close()
        return 0

    # Process in batches
    print(f"\nProcessing {missing_count} events in batches of {batch_size}...")

    processed = 0
    missing_ids_list = list(missing_ids)

    while processed < missing_count:
        # Get batch of event IDs to process
        batch_ids = missing_ids_list[processed:processed + batch_size]
        if not batch_ids:
            break

        # Fetch event details for this batch
        placeholders = ",".join("?" * len(batch_ids))
        cursor = events_conn.execute(f"""
            SELECT id, session_id, type, content, ts
            FROM events
            WHERE id IN ({placeholders})
            AND content IS NOT NULL
            AND content != ''
        """, batch_ids)

        batch = cursor.fetchall()
        if not batch:
            break

        # Extract texts for batch embedding
        texts = [row[3] for row in batch]

        # Generate embeddings
        try:
            embeddings = embedding_service.encode(texts)
        except Exception as e:
            print(f"\nERROR generating embeddings: {e}")
            break

        # Store embeddings
        for i, (event_id, session_id, event_type, content, timestamp) in enumerate(batch):
            if i < len(embeddings):
                embedding_storage.store(
                    event_id=event_id,
                    embedding=embeddings[i],
                    metadata={
                        "session_id": session_id,
                        "event_type": event_type,
                        "content": content[:1000],  # Store first 1000 chars for search display
                        "timestamp": timestamp,
                    }
                )

        processed += len(batch)

        # Progress update
        pct = (processed / missing_count) * 100
        print(f"  Processed {processed}/{missing_count} ({pct:.1f}%)")

    print(f"\n✓ Generated {processed} embeddings")

    # Verify final count
    cursor = embedding_storage.conn.execute("SELECT COUNT(*) FROM embeddings")
    final_count = cursor.fetchone()[0]
    print(f"✓ Total embeddings in database: {final_count}")

    events_conn.close()
    embedding_storage.close()

    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Backfill embeddings for existing events")
    parser.add_argument("--batch-size", type=int, default=100, help="Batch size for processing")
    parser.add_argument("--dry-run", action="store_true", help="Count events without generating embeddings")

    args = parser.parse_args()

    sys.exit(backfill_embeddings(batch_size=args.batch_size, dry_run=args.dry_run))

#!/usr/bin/env python3
"""
Bridge session entities from logging.db into Hippo knowledge graph.

Takes the lightweight NER entities extracted during Phase 2 and creates
corresponding nodes/edges in FalkorDB, connecting session knowledge to
the broader knowledge graph.

Also creates temporal proximity links between sessions that share entities.

Usage:
    uv run scripts/bridge_to_hippo.py [--dry-run] [--limit N]
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["redis"]
# ///

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import redis

# Hippo connection
HIPPO_HOST = "localhost"
HIPPO_PORT = 6380
HIPPO_GRAPH = "hippo"

# Logging DB
LOGGING_DB = Path.home() / ".claude" / "local" / "logging" / "-home-shawn" / "db" / "logging.db"

# Health
HEALTH_DIR = Path.home() / ".claude" / "local" / "health"


def get_redis():
    return redis.Redis(host=HIPPO_HOST, port=HIPPO_PORT, decode_responses=True)


def graph_query(r, query, params=None):
    """Execute a FalkorDB graph query.

    FalkorDB uses CYPHER prefix format for parameters:
    CYPHER key1="value1" key2=42 <actual cypher>
    """
    if params:
        param_str = "CYPHER " + " ".join(
            f"{k}={json.dumps(v)}" for k, v in params.items()
        ) + " "
        query = param_str + query
    return r.execute_command("GRAPH.QUERY", HIPPO_GRAPH, query)


def get_session_entities(db_path: Path, min_sessions: int = 2):
    """Get entities that appear in multiple sessions (high-value for graph).

    Returns dict of {entity_name: {type, sessions, total_mentions}}
    """
    conn = sqlite3.connect(str(db_path), timeout=10)
    cursor = conn.execute("""
        SELECT
            entity_name,
            entity_type,
            COUNT(DISTINCT session_id) as session_count,
            SUM(mention_count) as total_mentions
        FROM session_entities
        WHERE entity_type IN ('Person', 'Venture', 'Project')
        GROUP BY entity_name
        HAVING session_count >= ?
        ORDER BY session_count DESC
    """, (min_sessions,))

    entities = {}
    for row in cursor:
        entities[row[0]] = {
            "type": row[1],
            "sessions": row[2],
            "mentions": row[3],
        }
    conn.close()
    return entities


def get_session_entity_pairs(db_path: Path):
    """Get pairs of entities that co-occur in the same session.

    These become temporal proximity links — entities that appear in the same
    session are contextually related.

    Returns list of (entity1, entity2, shared_session_count)
    """
    conn = sqlite3.connect(str(db_path), timeout=10)
    cursor = conn.execute("""
        SELECT
            a.entity_name as e1,
            b.entity_name as e2,
            COUNT(DISTINCT a.session_id) as shared_sessions
        FROM session_entities a
        JOIN session_entities b ON a.session_id = b.session_id AND a.entity_name < b.entity_name
        WHERE a.entity_type IN ('Person', 'Venture', 'Project')
          AND b.entity_type IN ('Person', 'Venture', 'Project')
        GROUP BY a.entity_name, b.entity_name
        HAVING shared_sessions >= 3
        ORDER BY shared_sessions DESC
        LIMIT 500
    """)

    pairs = [(row[0], row[1], row[2]) for row in cursor]
    conn.close()
    return pairs


def ensure_entity_in_hippo(r, name: str, entity_type: str, session_count: int, mention_count: int):
    """Ensure an entity exists in Hippo with correct type and metadata."""
    # Map our types to Hippo's label system
    type_map = {
        "Person": "Person",
        "Venture": "Project",  # Hippo uses Project for ventures
        "Project": "Project",
    }
    label = type_map.get(entity_type, "Entity")
    ts = datetime.now(timezone.utc).isoformat()

    # MERGE: create if not exists, update metadata if exists
    cypher = f"""
    MERGE (n:{label} {{name: $name}})
    ON CREATE SET n.created = $ts, n.source = 'session-bridge', n.session_count = $sc, n.mention_count = $mc
    ON MATCH SET n.session_count = $sc, n.mention_count = $mc, n.last_accessed = $ts
    """
    try:
        graph_query(r, cypher, params={
            "name": name, "ts": ts, "sc": session_count, "mc": mention_count
        })
        return True
    except redis.exceptions.RedisError as e:
        print(f"  Warning: Failed to ensure {name}: {e}", file=sys.stderr)
        return False


def create_cooccurrence_edge(r, e1: str, e2: str, shared_sessions: int):
    """Create a CO_OCCURS_WITH edge between entities that share sessions."""
    ts = datetime.now(timezone.utc).isoformat()
    # Use label-constrained MATCH for performance (avoids full graph scan)
    cypher = """
    MATCH (a:Entity {name: $e1}), (b:Entity {name: $e2})
    MERGE (a)-[r:CO_OCCURS_WITH]->(b)
    ON CREATE SET r.weight = $weight, r.shared_sessions = $ss, r.created = $ts, r.source = 'session-bridge'
    ON MATCH SET r.weight = $weight, r.shared_sessions = $ss, r.last_accessed = $ts
    """
    try:
        graph_query(r, cypher, params={
            "e1": e1, "e2": e2, "weight": min(1.0, shared_sessions / 20.0),
            "ss": shared_sessions, "ts": ts
        })
        return True
    except redis.exceptions.RedisError as e:
        print(f"  Warning: Failed to link {e1} ↔ {e2}: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Bridge session entities to Hippo graph")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--limit", type=int, default=0, help="Max entities to process")
    parser.add_argument("--min-sessions", type=int, default=2, help="Min sessions for entity inclusion")
    args = parser.parse_args()

    if not LOGGING_DB.exists():
        print(f"Logging DB not found: {LOGGING_DB}")
        sys.exit(1)

    # Get entities
    entities = get_session_entities(LOGGING_DB, min_sessions=args.min_sessions)
    print(f"Entities with {args.min_sessions}+ sessions: {len(entities)}")

    # Get co-occurrence pairs
    pairs = get_session_entity_pairs(LOGGING_DB)
    print(f"Co-occurrence pairs (3+ shared sessions): {len(pairs)}")

    if args.dry_run:
        print("\n=== Top 20 entities ===")
        for name, info in list(entities.items())[:20]:
            print(f"  {name} ({info['type']}): {info['sessions']} sessions, {info['mentions']} mentions")
        print(f"\n=== Top 20 co-occurrence pairs ===")
        for e1, e2, ss in pairs[:20]:
            print(f"  {e1} ↔ {e2}: {ss} shared sessions")
        return

    # Connect to Hippo
    try:
        r = get_redis()
        r.ping()
    except redis.ConnectionError:
        print("Cannot connect to Hippo (FalkorDB) at localhost:6380")
        sys.exit(1)

    start = time.time()

    # Ensure entities exist in Hippo
    entity_count = 0
    upserted_names = set()
    entity_limit = args.limit if args.limit else len(entities)
    for name, info in list(entities.items())[:entity_limit]:
        if ensure_entity_in_hippo(r, name, info["type"], info["sessions"], info["mentions"]):
            entity_count += 1
            upserted_names.add(name)
        if entity_count % 50 == 0 and entity_count > 0:
            print(f"  [{entity_count}/{min(entity_limit, len(entities))}] entities processed")

    # Create co-occurrence edges (only for entities we successfully upserted)
    edge_count = 0
    for e1, e2, ss in pairs:
        if e1 not in upserted_names or e2 not in upserted_names:
            continue
        if create_cooccurrence_edge(r, e1, e2, ss):
            edge_count += 1

    elapsed = time.time() - start

    print(f"\nBridge complete in {elapsed:.1f}s")
    print(f"  Entities ensured: {entity_count}")
    print(f"  Co-occurrence edges: {edge_count}")

    # Write hippo heartbeat
    try:
        HEALTH_DIR.mkdir(parents=True, exist_ok=True)
        hb = HEALTH_DIR / "hippo-heartbeat"
        hb.write_text(f"{datetime.now(timezone.utc).isoformat()}\n")
    except OSError:
        pass


if __name__ == "__main__":
    main()

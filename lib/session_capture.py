"""
Session Knowledge Capture — Phase 2 of the Memory Roadmap.

Extracts session summaries, entities, and searchable text from session data.
Bridges the gap between raw transcripts (1.3GB, unsearchable) and indexed knowledge.

Three capture modes:
1. PostCompact hook — captures summary when Claude compacts context (real-time)
2. Session text extraction — extracts full text from transcript JSONL (batch)
3. Entity extraction — extracts named entities from session content (batch)
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


# Common entity patterns for lightweight NER (no LLM required)
def _load_person_patterns() -> list[str]:
    """Load contact names from relationship store for entity extraction.

    Reads from ~/.claude/local/relationships/relationships.db at import time.
    Falls back to a generic two-word capitalized name pattern if DB unavailable.
    """
    rel_db = Path.home() / ".claude/local/relationships/relationships.db"
    if rel_db.exists():
        try:
            conn = sqlite3.connect(str(rel_db))
            rows = conn.execute(
                "SELECT DISTINCT source FROM relationships UNION SELECT DISTINCT target FROM relationships"
            ).fetchall()
            conn.close()
            names = [r[0] for r in rows if r[0]]
            if names:
                # Longest names first to avoid partial matches
                names.sort(key=len, reverse=True)
                escaped = [re.escape(n).replace(r"\ ", r"\s?") for n in names]
                return [r"\b(?:" + "|".join(escaped) + r")\b"]
        except Exception:
            pass
    # Fallback: generic two-word capitalized name pattern
    return [r"\b[A-Z][a-z]{2,}\s[A-Z][a-z]{2,}\b"]


PERSON_PATTERNS = _load_person_patterns()


def _name_to_flexible_pattern(name: str) -> str:
    """Escape an entity name while allowing hyphen/space drift."""
    return re.escape(name).replace(r"\-", r"[-\s]?").replace(r"\ ", r"\s?")


def _load_venture_patterns() -> list[str]:
    """Load venture names from venture files for entity extraction.

    Reads from ~/.claude/local/ventures/*.md at import time.
    Falls back to empty list if no ventures found.
    """
    ventures_dir = Path.home() / ".claude/local/ventures"
    if ventures_dir.exists():
        try:
            names = [
                _name_to_flexible_pattern(f.stem)
                for f in ventures_dir.rglob("*.md")
                if f.stem not in ("README", "template")
            ]
            if names:
                names.sort(key=len, reverse=True)
                return [r"\b(?:" + "|".join(names) + r")\b"]
        except Exception:
            pass
    return []


def _load_project_patterns() -> list[str]:
    """Load plugin names for project entity extraction.

    Uses CLAUDE_PLUGIN_ROOT to find sibling plugins at import time.
    Falls back to empty list if not in a plugin context.
    """
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not plugin_root:
        return []
    # Sibling plugins live in the same parent directory
    plugins_dir = Path(plugin_root).parent
    if plugins_dir.exists():
        try:
            names = [re.escape(d.name) for d in plugins_dir.iterdir() if d.is_dir() and d.name.startswith("claude-")]
            if names:
                return [r"\b(?:" + "|".join(names) + r")\b"]
        except Exception:
            pass
    return []


VENTURE_PATTERNS = _load_venture_patterns()

PROJECT_PATTERNS = _load_project_patterns()

DATE_PATTERN = r"\b\d{4}-\d{2}-\d{2}\b"
MONEY_PATTERN = r"\$[\d,]+(?:\.\d{2})?"
HOURS_PATTERN = r"\b\d+(?:\.\d+)?\s*(?:hours?|hrs?|h)\b"


def extract_entities_lightweight(text: str) -> list[dict]:
    """Extract named entities using regex patterns.

    This is the lightweight fallback — no LLM required.
    Returns a list of {name, type, count} dicts.
    """
    entities = []
    seen = set()

    def add_matches(pattern: str, entity_type: str, case_sensitive: bool = False):
        flags = 0 if case_sensitive else re.IGNORECASE
        for m in re.finditer(pattern, text, flags):
            name = m.group().strip()
            # Normalize
            key = (name.lower(), entity_type)
            if key not in seen:
                count = len(re.findall(re.escape(name), text, re.IGNORECASE))
                entities.append(
                    {
                        "name": name,
                        "type": entity_type,
                        "count": count,
                    }
                )
                seen.add(key)

    for pattern in PERSON_PATTERNS:
        add_matches(pattern, "Person", case_sensitive=True)
    for pattern in VENTURE_PATTERNS:
        add_matches(pattern, "Venture")
    for pattern in PROJECT_PATTERNS:
        add_matches(pattern, "Project")
    add_matches(DATE_PATTERN, "Date")
    add_matches(MONEY_PATTERN, "Money")
    add_matches(HOURS_PATTERN, "Duration")

    return entities


def store_session_summary(
    db_path: Path,
    session_id: str,
    summary: str,
    source: str = "compact",
    entities: list[dict] | None = None,
) -> None:
    """Store a session summary and its extracted entities.

    Args:
        db_path: Path to logging.db
        session_id: Session UUID
        summary: The compact summary or extracted text
        source: Where the summary came from ("compact", "extracted", "manual")
        entities: Optional pre-extracted entities; if None, will extract from summary
    """
    if entities is None:
        entities = extract_entities_lightweight(summary)

    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")

    try:
        # Upsert session summary
        conn.execute(
            """
            INSERT OR REPLACE INTO session_summaries
            (session_id, summary, source, entities_extracted, created_at)
            VALUES (?, ?, ?, ?, ?)
        """,
            (
                session_id,
                summary,
                source,
                len(entities),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

        # Upsert entities
        now = datetime.now(timezone.utc).isoformat()
        for ent in entities:
            conn.execute(
                """
                INSERT INTO session_entities
                (session_id, entity_name, entity_type, mention_count, first_seen, context)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, entity_name) DO UPDATE SET
                    mention_count = excluded.mention_count,
                    entity_type = CASE
                        WHEN excluded.entity_type != 'unknown' THEN excluded.entity_type
                        ELSE session_entities.entity_type
                    END
            """,
                (
                    session_id,
                    ent["name"],
                    ent.get("type", "unknown"),
                    ent.get("count", 1),
                    now,
                    None,  # context can be added later
                ),
            )

        conn.commit()
    finally:
        conn.close()


def process_postcompact_summary(
    db_path: Path,
    session_id: str,
    summary: str,
) -> int:
    """Process a PostCompact summary: store it and extract entities.

    Returns: number of entities extracted.
    """
    entities = extract_entities_lightweight(summary)
    store_session_summary(db_path, session_id, summary, "compact", entities)
    return len(entities)


def batch_extract_from_transcripts(
    project_dir: str = str(Path.home()),
    db_path: Path | None = None,
    limit: int = 0,
) -> dict:
    """Batch extract text and entities from all session transcripts.

    This is the heavy-lift operation that processes the full 1.3GB of transcripts.
    Run it once to backfill, then rely on PostCompact for incremental capture.

    Args:
        project_dir: Project directory path
        db_path: Path to logging.db (auto-detected if None)
        limit: Max sessions to process (0 = all)

    Returns:
        Stats dict with counts
    """
    scripts_dir = Path(__file__).parent.parent / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from extract_session_text import extract_session, iter_transcripts

    if db_path is None:
        from lib import encode_project_path

        encoded = encode_project_path(project_dir)
        db_path = Path.home() / ".claude" / "local" / "logging" / encoded / "db" / "logging.db"

    # Check which sessions already have summaries
    conn = sqlite3.connect(str(db_path), timeout=10)
    cursor = conn.execute("SELECT session_id FROM session_summaries")
    already_processed = {row[0] for row in cursor}
    conn.close()

    stats = {
        "processed": 0,
        "skipped": 0,
        "entities_total": 0,
        "chars_total": 0,
        "errors": 0,
    }

    for path in iter_transcripts(project_dir):
        session_id = path.stem
        if session_id in already_processed:
            stats["skipped"] += 1
            continue

        if limit and stats["processed"] >= limit:
            break

        try:
            result = extract_session(path)
            if result is None or result.total_chars < 100:
                stats["skipped"] += 1
                continue

            # ZERO TRUNCATION: store full user text. Storage layer handles
            # large blobs via TEXT column (SQLite max 1GB per cell).
            text = result.user_text
            entities = extract_entities_lightweight(result.full_text)
            store_session_summary(db_path, session_id, text, "extracted", entities)

            stats["processed"] += 1
            stats["entities_total"] += len(entities)
            stats["chars_total"] += result.total_chars

        except Exception as e:
            stats["errors"] += 1
            print(f"  Error processing {session_id}: {e}", file=sys.stderr)

    return stats


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Session knowledge capture")
    parser.add_argument("--batch", action="store_true", help="Batch extract from all transcripts")
    parser.add_argument("--limit", type=int, default=0, help="Max sessions to process (0 = all)")
    parser.add_argument("--project-dir", default=str(Path.home()), help="Project directory (default: $HOME)")
    args = parser.parse_args()

    if args.batch:
        print("Starting batch extraction...")
        stats = batch_extract_from_transcripts(
            project_dir=args.project_dir,
            limit=args.limit,
        )
        print(f"Results: {json.dumps(stats, indent=2)}")
    else:
        parser.print_help()

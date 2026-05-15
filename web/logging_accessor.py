"""LoggingAccessor — implements claude_webui.Accessor over the logging corpus.

task-508 Phase 2 — kernel-webui satellite migration. Replaces the Next.js
renderer (now quarantined in web-legacy/) with vanilla stdlib + Accessor
Protocol pattern.

Data sources:
  - Per-project DBs at ~/.claude/local/logging/<slug>/db/logging.db
  - Cross-project rolled-up index at ~/.claude/local/logging/_index/index.db
  - Live JSONL streams at ~/.claude/local/logging/<slug>/sessions/*.jsonl (Phase 7)

Five Accessor methods map to kernel routes:
    GET /api/list           → list(params)        — prompts list (default) or sessions list
    GET /api/detail/<id>    → detail(item_id)     — single session transcript or single prompt
    GET /api/stats          → stats()             — corpus health summary
    GET /api/feed           → feed(params)        — chrono-aggregator slice (claude-feed integration)
    GET /healthz            → healthz()           — pipeline heartbeat + DB integrity

Extra GET routes via LoggingHandler subclass (Phase 2.3):
    GET /api/prompts        → list of prompts cross-project, reverse-chrono
    GET /api/sessions       → list of sessions cross-project
    GET /api/events         → list of events filtered by project/session/type
    GET /api/search         → three-mode FTS5 search (prompts / events / semantic)
    GET /api/transcript     → single-session events (clean or full mode)
    GET /api/projects       → list of all project slugs with metadata
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

NAMESPACE: str = "legion.claude-logging"

LOGGING_ROOT = Path.home() / ".claude" / "local" / "logging"
INDEX_DB = LOGGING_ROOT / "_index" / "index.db"

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


def _open_ro(db_path: Path) -> sqlite3.Connection:
    """Open SQLite DB read-only with row factory."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    con.row_factory = sqlite3.Row
    return con


class LoggingAccessor:
    """Accessor over the claude-logging cross-project corpus.

    All queries read from the central `_index/index.db` first; per-project
    DB reads only happen for single-session transcripts (where the cross-
    project preview is insufficient).
    """

    def __init__(self, logging_root: Path | None = None) -> None:
        self.root = logging_root or LOGGING_ROOT
        self.index_db = self.root / "_index" / "index.db"
        self._start_time = time.time()

    # ── Accessor Protocol ───────────────────────────────────────────

    def list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Default list view — recent prompts cross-project, reverse-chrono."""
        return self.prompts(params)

    def detail(self, item_id: str) -> dict[str, Any]:
        """Default detail — a single session transcript (clean mode)."""
        return self.transcript({"session": item_id, "mode": "clean"})

    def stats(self) -> dict[str, Any]:
        """Corpus health summary across all projects."""
        if not self.index_db.exists():
            return {
                "key_metric": 0,
                "key_metric_label": "prompts",
                "projects": 0,
                "events": 0,
                "events_by_type": {},
                "error": "index DB missing",
            }
        con = _open_ro(self.index_db)
        try:
            event_count = con.execute("SELECT COUNT(*) FROM events_index").fetchone()[0]
            project_count = con.execute("SELECT COUNT(DISTINCT project_slug) FROM events_index").fetchone()[0]
            prompt_count = con.execute(
                "SELECT COUNT(*) FROM events_index WHERE type = 'UserPromptSubmit'"
            ).fetchone()[0]
            session_count = con.execute(
                "SELECT COUNT(DISTINCT session_id) FROM events_index"
            ).fetchone()[0]
            type_counts = dict(con.execute(
                "SELECT type, COUNT(*) FROM events_index GROUP BY type ORDER BY COUNT(*) DESC"
            ).fetchall())
            last_synced = con.execute(
                "SELECT MAX(last_synced_at) FROM rollup_state"
            ).fetchone()[0]
            return {
                "key_metric": prompt_count,
                "key_metric_label": "prompts",
                "projects": project_count,
                "sessions": session_count,
                "events": event_count,
                "events_by_type": type_counts,
                "last_synced_at": last_synced,
            }
        finally:
            con.close()

    def feed(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Chrono-aggregator slice (claude-feed integration).

        Returns recent events across all projects in reverse-chrono with
        unified item shape: {id, type, ts, source, summary, project_slug}.
        """
        limit = min(int(params.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
        since = params.get("since")
        if not self.index_db.exists():
            return []
        con = _open_ro(self.index_db)
        try:
            if since:
                cursor = con.execute(
                    "SELECT event_id, project_slug, session_id, type, ts, persona, content_preview "
                    "FROM events_index WHERE ts > ? AND type IN ('UserPromptSubmit','AssistantResponse','SubagentStop','Stop') "
                    "ORDER BY ts DESC LIMIT ?",
                    (str(since), limit),
                )
            else:
                cursor = con.execute(
                    "SELECT event_id, project_slug, session_id, type, ts, persona, content_preview "
                    "FROM events_index WHERE type IN ('UserPromptSubmit','AssistantResponse','SubagentStop','Stop') "
                    "ORDER BY ts DESC LIMIT ?",
                    (limit,),
                )
            return [
                {
                    "id": row["event_id"],
                    "type": row["type"],
                    "ts": row["ts"],
                    "source": NAMESPACE,
                    "project_slug": row["project_slug"],
                    "session_id": row["session_id"],
                    "persona": row["persona"],
                    "summary": (row["content_preview"] or "")[:200],
                }
                for row in cursor
            ]
        finally:
            con.close()

    def healthz(self) -> dict[str, Any]:
        """Health check: index DB present + recent rollup + heartbeat fresh."""
        ok = True
        issues = []
        index_ok = self.index_db.exists()
        if not index_ok:
            ok = False
            issues.append(f"index DB missing at {self.index_db}")

        event_count = 0
        project_count = 0
        last_synced = None
        rollup_age_s = None
        if index_ok:
            try:
                con = _open_ro(self.index_db)
                event_count = con.execute("SELECT COUNT(*) FROM events_index").fetchone()[0]
                project_count = con.execute(
                    "SELECT COUNT(DISTINCT project_slug) FROM events_index"
                ).fetchone()[0]
                last_synced = con.execute(
                    "SELECT MAX(last_synced_at) FROM rollup_state"
                ).fetchone()[0]
                con.close()
                if last_synced:
                    from datetime import datetime
                    try:
                        synced_dt = datetime.fromisoformat(last_synced.replace("Z", "+00:00"))
                        rollup_age_s = (time.time() - synced_dt.timestamp())
                        if rollup_age_s > 3600:
                            issues.append(f"rollup stale: {rollup_age_s:.0f}s since last sync")
                    except (ValueError, AttributeError):
                        pass
            except Exception as e:
                ok = False
                issues.append(f"index DB query failed: {e}")

        # Heartbeat check
        heartbeat = self.root.parent / "health" / "logging.json"
        heartbeat_age_s = None
        if heartbeat.exists():
            try:
                heartbeat_age_s = time.time() - heartbeat.stat().st_mtime
                if heartbeat_age_s > 86400:
                    issues.append(f"capture heartbeat stale: {heartbeat_age_s:.0f}s")
            except Exception:
                pass

        return {
            "ok": ok,
            "namespace": NAMESPACE,
            "database": str(self.index_db),
            "elapsed_ms": int((time.time() - self._start_time) * 1000),
            "stats": {
                "key_metric": event_count,
                "key_metric_label": "events",
                "projects": project_count,
                "rollup_age_s": rollup_age_s,
                "heartbeat_age_s": heartbeat_age_s,
            },
            "issues": issues,
        }

    # ── Extra routes (consumed by LoggingHandler subclass) ──────────

    def prompts(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Reverse-chrono cross-project prompt list.

        Filter chips supported (passed as params):
          - persona: filter to specific persona slug
          - project_slug: filter to specific project
          - q: FTS5 search term applied to content_preview
          - limit, offset
        """
        limit = min(int(params.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
        offset = int(params.get("offset", 0))
        persona = params.get("persona")
        project = params.get("project_slug")
        q = (params.get("q") or "").strip()

        if not self.index_db.exists():
            return []

        con = _open_ro(self.index_db)
        try:
            where_clauses = ["type = 'UserPromptSubmit'"]
            args: list[Any] = []
            if persona:
                where_clauses.append("persona = ?")
                args.append(persona)
            if project:
                where_clauses.append("project_slug = ?")
                args.append(project)

            # FTS5 path if q present
            if q:
                # Same FTS5 token-quoting as in search() — neutralize hyphens
                tokens = q.split()
                fts_query = " ".join('"' + t.replace('"', '""') + '"' for t in tokens) if tokens else q
                cursor = con.execute(
                    f"SELECT ei.event_id, ei.project_slug, ei.session_id, ei.type, ei.ts, "
                    f"ei.persona, ei.content_preview, ei.has_full_content "
                    f"FROM events_index_fts fts "
                    f"JOIN events_index ei ON ei.event_id = fts.event_id "
                    f"WHERE fts.events_index_fts MATCH ? AND {' AND '.join(where_clauses)} "
                    f"ORDER BY ei.ts DESC LIMIT ? OFFSET ?",
                    (fts_query, *args, limit, offset),
                )
            else:
                cursor = con.execute(
                    f"SELECT event_id, project_slug, session_id, type, ts, "
                    f"persona, content_preview, has_full_content "
                    f"FROM events_index WHERE {' AND '.join(where_clauses)} "
                    f"ORDER BY ts DESC LIMIT ? OFFSET ?",
                    (*args, limit, offset),
                )

            return [self._prompt_row(row) for row in cursor]
        finally:
            con.close()

    def _prompt_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "project_slug": row["project_slug"],
            "session_id": row["session_id"],
            "type": row["type"],
            "ts": row["ts"],
            "persona": row["persona"],
            "preview": row["content_preview"],
            "has_full": bool(row["has_full_content"]),
        }

    def sessions(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Cross-project session list, reverse-chrono by latest event."""
        limit = min(int(params.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
        offset = int(params.get("offset", 0))
        project = params.get("project_slug")

        if not self.index_db.exists():
            return []

        con = _open_ro(self.index_db)
        try:
            where = "WHERE project_slug = ?" if project else ""
            args = (project,) if project else ()
            cursor = con.execute(
                f"SELECT session_id, project_slug, MIN(ts) AS started_at, MAX(ts) AS ended_at, "
                f"COUNT(*) AS event_count FROM events_index {where} "
                f"GROUP BY session_id, project_slug "
                f"ORDER BY ended_at DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            )
            return [dict(row) for row in cursor]
        finally:
            con.close()

    def transcript(self, params: dict[str, Any]) -> dict[str, Any]:
        """Single-session transcript. Modes: 'clean' (prompts + responses only)
        or 'full' (all events)."""
        session_id = params.get("session", params.get("session_id"))
        mode = params.get("mode", "clean")
        if not session_id:
            return {"error": "session_id required"}
        if not self.index_db.exists():
            return {"error": "index DB missing"}

        # Find the project_slug for this session
        idx_con = _open_ro(self.index_db)
        try:
            row = idx_con.execute(
                "SELECT project_slug FROM events_index WHERE session_id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
            if not row:
                return {"error": f"session not found: {session_id}"}
            project_slug = row["project_slug"]
        finally:
            idx_con.close()

        # Read full events from per-project DB
        proj_db = self.root / project_slug / "db" / "logging.db"
        if not proj_db.exists():
            return {"error": f"project DB missing: {proj_db}"}

        if mode == "clean":
            type_filter = "AND type IN ('UserPromptSubmit', 'AssistantResponse')"
        else:
            type_filter = ""

        proj_con = _open_ro(proj_db)
        try:
            cursor = proj_con.execute(
                f"SELECT id, type, ts, persona, agent_id, tool_name, content, data "
                f"FROM events WHERE session_id = ? {type_filter} ORDER BY ts ASC",
                (session_id,),
            )
            events = []
            for row in cursor:
                try:
                    data = json.loads(row["data"]) if row["data"] else {}
                except json.JSONDecodeError:
                    data = {}
                events.append({
                    "event_id": row["id"],
                    "type": row["type"],
                    "ts": row["ts"],
                    "persona": row["persona"],
                    "agent_id": row["agent_id"],
                    "tool_name": row["tool_name"],
                    "content": row["content"],
                    "data": data,
                })
            return {
                "session_id": session_id,
                "project_slug": project_slug,
                "mode": mode,
                "event_count": len(events),
                "events": events,
            }
        finally:
            proj_con.close()

    def projects(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List all project slugs with metadata.

        Sorted by recent activity (max ts desc).
        """
        if not self.index_db.exists():
            return []
        con = _open_ro(self.index_db)
        try:
            cursor = con.execute(
                "SELECT project_slug, COUNT(*) AS event_count, MAX(ts) AS last_event_ts "
                "FROM events_index GROUP BY project_slug ORDER BY last_event_ts DESC"
            )
            return [dict(row) for row in cursor]
        finally:
            con.close()

    def search(self, params: dict[str, Any]) -> dict[str, Any]:
        """Three-mode search:
          - mode=prompts: FTS5 on prompts only
          - mode=events: FTS5 across all event types
          - mode=semantic: not yet implemented (Phase 5+ embeddings)
        """
        mode = params.get("mode", "prompts")
        q = (params.get("q") or "").strip()
        limit = min(int(params.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
        offset = int(params.get("offset", 0))

        if not q:
            return {"mode": mode, "q": "", "results": []}
        if not self.index_db.exists():
            return {"mode": mode, "q": q, "results": [], "error": "index DB missing"}

        con = _open_ro(self.index_db)
        try:
            # FTS5 query: wrap each token in double-quotes to neutralize
            # hyphens/special-chars (e.g. "task-508" would otherwise parse the
            # `-` as a NOT operator). Multi-token queries become space-joined
            # quoted phrases which FTS5 treats as implicit AND.
            tokens = q.split()
            fts_query = " ".join('"' + t.replace('"', '""') + '"' for t in tokens) if tokens else q
            if mode == "prompts":
                type_filter = "AND ei.type = 'UserPromptSubmit'"
            elif mode == "events":
                type_filter = ""
            elif mode == "semantic":
                return {"mode": mode, "q": q, "results": [], "error": "semantic search Phase 5+"}
            else:
                type_filter = ""

            cursor = con.execute(
                f"SELECT ei.event_id, ei.project_slug, ei.session_id, ei.type, ei.ts, "
                f"ei.persona, ei.content_preview, ei.has_full_content "
                f"FROM events_index_fts fts "
                f"JOIN events_index ei ON ei.event_id = fts.event_id "
                f"WHERE fts.events_index_fts MATCH ? {type_filter} "
                f"ORDER BY ei.ts DESC LIMIT ? OFFSET ?",
                (fts_query, limit, offset),
            )
            results = [self._prompt_row(row) for row in cursor]
            return {"mode": mode, "q": q, "results": results, "count": len(results)}
        finally:
            con.close()

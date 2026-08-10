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
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    from .claude_web_sessions import get_transcript as get_claude_web_transcript
    from .claude_web_sessions import list_sessions as list_claude_web_sessions
    from .claude_web_sessions import search_sessions as search_claude_web_sessions
except ImportError:  # direct script loading by the stdlib Web UI launcher
    from claude_web_sessions import get_transcript as get_claude_web_transcript
    from claude_web_sessions import list_sessions as list_claude_web_sessions
    from claude_web_sessions import search_sessions as search_claude_web_sessions
try:
    from .pi_sessions import session_graph as get_pi_session_graph
except ImportError:
    from pi_sessions import session_graph as get_pi_session_graph
try:
    from lib.session_titles import read_titles
except ImportError:
    from session_titles import read_titles

NAMESPACE: str = "legion.claude-logging"

LOGGING_ROOT = Path.home() / ".claude" / "local" / "logging"
INDEX_DB = LOGGING_ROOT / "_index" / "index.db"
NATIVE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
PI_ROOT = Path.home() / ".pi" / "agent"
PI_SESSIONS_ROOT = PI_ROOT / "sessions"
PI_EXTENSION = Path(__file__).resolve().parents[1] / "adapters" / "pi" / "extension.ts"
PRIME_ROOT = Path.home() / ".prime" / "agent"
PRIME_SESSIONS_ROOT = PRIME_ROOT / "sessions"
PRIME_EXTENSION = Path(__file__).resolve().parents[1] / "adapters" / "prime_agent" / "extension.ts"
OMP_ROOT = Path.home() / ".omp" / "agent"
OMP_SESSIONS_ROOT = OMP_ROOT / "sessions"
OMP_EXTENSION = Path(__file__).resolve().parents[1] / "adapters" / "omp" / "extension.ts"
HERMES_DB = Path.home() / ".hermes" / "state.db"
HERMES_CONFIG = Path.home() / ".hermes" / "config.yaml"
HERMES_HOOK = Path(__file__).resolve().parents[1] / "adapters" / "hermes" / "log_event.py"
IDENTITY_DB = Path.home() / ".local/state/legion/identity/identity.db"
COLOURS_DB = Path.home() / ".local/state/legion/colours/colours.db"
EMOJI_DB = Path.home() / ".local/state/legion/emoji/emoji.db"
AGENTS_DB = Path.home() / ".claude/local/agents/registry.db"

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


def _open_ro(db_path: Path) -> sqlite3.Connection:
    """Open SQLite DB read-only with row factory."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    con.row_factory = sqlite3.Row
    return con


def _sqlite_utc_timestamp(value: str) -> float:
    """Parse SQLite CURRENT_TIMESTAMP values as UTC, not host-local time."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


@lru_cache(maxsize=4096)
def _native_tail_metadata(path_str: str, mtime_ns: int) -> tuple[str, str]:
    """Return the latest native AI title and user/assistant message timestamp."""
    del mtime_ns  # cache-key invalidation only
    path = Path(path_str)
    title = ""
    latest_message = ""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - 2 * 1024 * 1024))
            if handle.tell():
                handle.readline()  # discard a partial first line
            lines = handle.read().splitlines()
        for raw in reversed(lines):
            try:
                item = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            kind = item.get("type")
            if not latest_message and kind in ("user", "assistant"):
                latest_message = item.get("timestamp", "")
            if not title and kind == "ai-title":
                title = item.get("aiTitle", "")
            if title and latest_message:
                break
    except OSError:
        pass
    return title, latest_message


@lru_cache(maxsize=8)
def _native_session_paths(root_mtime_ns: int) -> dict[str, Path]:
    del root_mtime_ns
    return {path.stem: path for path in NATIVE_PROJECTS_ROOT.glob("*/*.jsonl")}


def _catalog_native_metadata(session_id: str) -> tuple[str, str, str]:
    try:
        paths = _native_session_paths(NATIVE_PROJECTS_ROOT.stat().st_mtime_ns)
        path = paths.get(session_id)
        if not path:
            return "", "", ""
        title, latest = _native_tail_metadata(str(path), path.stat().st_mtime_ns)
        return title, latest, path.parent.name
    except OSError:
        return "", "", ""


class LoggingAccessor:
    """Accessor over the claude-logging cross-project corpus.

    All queries read from the central `_index/index.db` first; per-project
    DB reads only happen for single-session transcripts (where the cross-
    project preview is insufficient).
    """

    def __init__(self, logging_root: Path | None = None, *, claude_web_enabled: bool | None = None,
                 identity_path: Path | None = None, colours_path: Path | None = None,
                 emoji_path: Path | None = None, agents_path: Path | None = None) -> None:
        self.root = logging_root or LOGGING_ROOT
        self.index_db = self.root / "_index" / "index.db"
        self.title_db = self.root / "_index" / "session-metadata.db"
        self.claude_web_enabled = logging_root is None if claude_web_enabled is None else claude_web_enabled
        self.identity_path = identity_path or IDENTITY_DB
        self.colours_path = colours_path or COLOURS_DB
        self.emoji_path = emoji_path or EMOJI_DB
        self.agents_path = agents_path or AGENTS_DB
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
                "SELECT COUNT(*) FROM events_index WHERE type = 'UserPromptSubmit' "
                "AND COALESCE(is_synthetic, 0) = 0"
            ).fetchone()[0]
            session_count = con.execute(
                "SELECT COUNT(DISTINCT session_id) FROM events_index"
            ).fetchone()[0]
            type_counts = dict(con.execute(
                "SELECT type, COUNT(*) FROM events_index GROUP BY type ORDER BY COUNT(*) DESC"
            ).fetchall())
            runtime_counts = dict(con.execute(
                "SELECT runtime, COUNT(*) FROM events_index GROUP BY runtime ORDER BY COUNT(*) DESC"
            ).fetchall())
            source_counts = dict(con.execute(
                "SELECT source_kind, COUNT(*) FROM events_index GROUP BY source_kind ORDER BY COUNT(*) DESC"
            ).fetchall())
            last_synced = con.execute(
                "SELECT MAX(last_synced_at) FROM rollup_state"
            ).fetchone()[0]
            web = list_claude_web_sessions(100_000, 0) if self.claude_web_enabled else []
            web_events = sum(int(item.get("event_count") or 0) for item in web)
            if web:
                runtime_counts["claude-web"] = web_events
                source_counts["archive"] = web_events
                type_counts["ArchivedMessage"] = web_events
            return {
                "key_metric": prompt_count,
                "key_metric_label": "prompts",
                "projects": project_count + (1 if web else 0),
                "sessions": session_count + len(web),
                "events": event_count + web_events,
                "events_by_type": type_counts,
                "events_by_runtime": runtime_counts,
                "events_by_source_kind": source_counts,
                "last_synced_at": last_synced,
                "adapter_health": self._adapter_health(con),
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
        """Health check: index DB present + real-time rollup daemon fresh."""
        ok = True
        issues = []
        index_ok = self.index_db.exists()
        if not index_ok:
            ok = False
            issues.append(f"index DB missing at {self.index_db}")

        event_count = 0
        project_count = 0
        runtime_counts: dict[str, int] = {}
        adapter_health: dict[str, Any] = {}
        last_synced = None
        rollup_age_s = None
        if index_ok:
            try:
                con = _open_ro(self.index_db)
                event_count = con.execute("SELECT COUNT(*) FROM events_index").fetchone()[0]
                project_count = con.execute(
                    "SELECT COUNT(DISTINCT project_slug) FROM events_index"
                ).fetchone()[0]
                runtime_counts = dict(con.execute(
                    "SELECT runtime, COUNT(*) FROM events_index GROUP BY runtime"
                ).fetchall())
                adapter_health = self._adapter_health(con)
                last_synced = con.execute(
                    "SELECT MAX(last_synced_at) FROM rollup_state"
                ).fetchone()[0]
                con.close()
                if last_synced:
                    try:
                        rollup_age_s = max(0.0, time.time() - _sqlite_utc_timestamp(last_synced))
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

        daemon_health = self.root / "_index" / "daemon-health.json"
        daemon_age_s = None
        daemon_degraded_count = None
        if not daemon_health.exists():
            ok = False
            issues.append("real-time rollup daemon heartbeat missing")
        else:
            try:
                daemon_age_s = max(0.0, time.time() - daemon_health.stat().st_mtime)
                daemon_data = json.loads(daemon_health.read_text())
                daemon_degraded_count = int(daemon_data.get("degraded_count", 0))
                if daemon_age_s > 30:
                    ok = False
                    issues.append(f"real-time rollup daemon stale: {daemon_age_s:.0f}s")
                if daemon_degraded_count:
                    ok = False
                    issues.append(f"real-time rollup has {daemon_degraded_count} degraded shards")
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
                ok = False
                issues.append(f"real-time rollup health unreadable: {e}")

        result = {
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
                "daemon_age_s": daemon_age_s,
                "daemon_degraded_count": daemon_degraded_count,
                "events_by_runtime": runtime_counts,
                "adapters": adapter_health,
            },
            "issues": issues,
        }
        return result

    def _adapter_health(self, con: sqlite3.Connection) -> dict[str, Any]:
        """Capability-aware source discovery versus indexed state."""
        indexed = {
            row["runtime"]: {"events": row["events"], "sessions": row["sessions"], "last_event": row["last_event"]}
            for row in con.execute(
                "SELECT runtime, COUNT(*) events, COUNT(DISTINCT session_id) sessions, MAX(ts) last_event "
                "FROM events_index GROUP BY runtime"
            )
        }
        try:
            web_sessions = len(json.loads((Path.home() / ".claude/local/claude-claude-web/projection/conversations/index.json").read_text()))
        except (OSError, json.JSONDecodeError, TypeError):
            web_sessions = 0
        try:
            pi_settings = json.loads((PI_ROOT / "settings.json").read_text())
        except (OSError, json.JSONDecodeError):
            pi_settings = {}
        configured_extensions = [str(value) for value in pi_settings.get("extensions", [])]
        try:
            prime_settings = json.loads((PRIME_ROOT / "settings.json").read_text())
        except (OSError, json.JSONDecodeError):
            prime_settings = {}
        prime_extensions = [str(value) for value in prime_settings.get("extensions", [])]
        try:
            omp_config = (OMP_ROOT / "config.yml").read_text()
        except OSError:
            omp_config = ""
        try:
            pi_auth_ready = bool(json.loads((PI_ROOT / "auth.json").read_text()))
        except (OSError, json.JSONDecodeError):
            pi_auth_ready = False

        def state(runtime: str, discovered: int, **extra: Any) -> dict[str, Any]:
            row = indexed.get(runtime, {"events": 0, "sessions": 0, "last_event": None})
            return {"runtime": runtime, "discovered_sessions": discovered, "indexed_sessions": row["sessions"], "indexed_events": row["events"], "last_event": row["last_event"], **extra}

        hermes_sessions = 0
        if HERMES_DB.exists():
            try:
                hermes_con = _open_ro(HERMES_DB)
                hermes_sessions = int(hermes_con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
                hermes_con.close()
            except sqlite3.Error:
                pass
        try:
            hermes_config = HERMES_CONFIG.read_text()
        except OSError:
            hermes_config = ""
        try:
            hermes_allowlist = json.loads((Path.home() / ".hermes" / "shell-hooks-allowlist.json").read_text())
            hermes_approved = str(HERMES_HOOK) in json.dumps(hermes_allowlist)
        except (OSError, json.JSONDecodeError):
            hermes_approved = False

        result = {
            "claude": state("claude", len(list(NATIVE_PROJECTS_ROOT.glob("*/*.jsonl"))), live=True, archive=True),
            "codex": state("codex", len(list(CODEX_SESSIONS_ROOT.rglob("*.jsonl"))) if CODEX_SESSIONS_ROOT.exists() else 0, live=True, archive=True),
            "claude-web": state("claude-web", web_sessions, live=False, archive=True),
            "pi": state(
                "pi", len(list(PI_SESSIONS_ROOT.rglob("*.jsonl"))) if PI_SESSIONS_ROOT.exists() else 0,
                live=True, archive=True, installed=(Path.home() / ".local/bin/pi").exists(),
                extension_configured=str(PI_EXTENSION) in configured_extensions,
                credentials_configured=pi_auth_ready,
            ),
            "prime-agent": state(
                "prime-agent", len(list(PRIME_SESSIONS_ROOT.rglob("*.jsonl"))) if PRIME_SESSIONS_ROOT.exists() else 0,
                live=True, archive=True, installed=(Path.home() / ".local/bin/prime-agent").exists(),
                extension_configured=str(PRIME_EXTENSION) in prime_extensions,
            ),
            "omp": state(
                "omp", len(list(OMP_SESSIONS_ROOT.rglob("*.jsonl"))) if OMP_SESSIONS_ROOT.exists() else 0,
                live=True, archive=True, installed=(Path.home() / ".cache/.bun/bin/omp").exists(),
                extension_configured=str(OMP_EXTENSION) in omp_config,
            ),
            "hermes": state(
                "hermes", hermes_sessions,
                live=True, archive=True, installed=(Path.home() / ".local/bin/hermes").exists(),
                extension_configured=str(HERMES_HOOK) in hermes_config and hermes_approved,
            ),
        }
        return result

    # ── Extra routes (consumed by LoggingHandler subclass) ──────────

    def personas(self) -> list[dict[str, Any]]:
        """Canonical persona catalogue decorated by shared emoji and colours."""
        if not self.identity_path.is_file():
            return []
        counts: dict[str, int] = {}
        if self.index_db.is_file():
            with _open_ro(self.index_db) as con:
                counts = {str(row[0]): int(row[1]) for row in con.execute(
                    "SELECT persona,count(*) FROM events_index WHERE persona IS NOT NULL GROUP BY persona"
                )}
        colours: dict[str, tuple[str, str]] = {}
        if self.colours_path.is_file():
            with _open_ro(self.colours_path) as con:
                colours = {str(row[0]): (str(row[1]), str(row[2])) for row in con.execute(
                    "SELECT entity_key,hex,assigned_by FROM colours WHERE entity_key LIKE 'persona:%'"
                )}
        emoji: dict[str, str] = {}
        if self.emoji_path.is_file():
            with _open_ro(self.emoji_path) as con:
                emoji = {str(row[0]): str(row[1]) for row in con.execute(
                    "SELECT entity_key,emoji FROM emoji_mappings WHERE entity_key LIKE 'persona:%'"
                )}
        with _open_ro(self.identity_path) as con:
            rows = con.execute(
                "SELECT id,display_name,owner_ref,metadata_json FROM principals "
                "WHERE kind='persona' AND disabled_at IS NULL ORDER BY display_name COLLATE NOCASE"
            ).fetchall()
        result = []
        for row in rows:
            key = str(row["id"])
            slug = key.removeprefix("persona:")
            metadata = json.loads(row["metadata_json"] or "{}")
            colour = colours.get(key)
            result.append({
                "key": key, "slug": slug, "label": row["display_name"],
                "owner_ref": row["owner_ref"],
                "emoji": emoji.get(key) or metadata.get("glyph") or metadata.get("emoji") or "◆",
                "colour": {
                    "hex": colour[0] if colour else "#6c7086",
                    "source": "claude-colours" if colour else "neutral-fallback",
                    "assigned_by": colour[1] if colour else None,
                },
                "observed_events": counts.get(slug, 0),
                "identity_source": "legion-identity",
            })
        return result

    def _session_identity(self, session_id: str, runtimes: list[str],
                          project_slugs: list[str]) -> dict[str, Any]:
        refs = [
            f"logging:{runtime}:{project}:{session_id}"
            for runtime in runtimes for project in project_slugs
        ]
        namespaces = {
            "claude": "harness:claude-code", "codex": "harness:codex",
            "prime-agent": "harness:prime-agent", "pi": "harness:pi",
            "omp": "harness:omp", "hermes": "harness:hermes",
        }
        resolved: set[str] = set()
        evidence: list[dict[str, str]] = []
        if self.agents_path.is_file():
            with _open_ro(self.agents_path) as con:
                for runtime in runtimes:
                    namespace = namespaces.get(runtime)
                    if not namespace:
                        continue
                    row = con.execute(
                        """SELECT session_id,confidence,source FROM session_aliases
                           WHERE namespace=? AND external_id=? AND retracted_at IS NULL""",
                        (namespace, session_id),
                    ).fetchone()
                    if row:
                        resolved.add(str(row["session_id"]))
                        evidence.append({
                            "namespace": namespace, "external_id": session_id,
                            "confidence": str(row["confidence"]), "source": str(row["source"]),
                        })
        state = "resolved" if len(resolved) == 1 else "ambiguous" if resolved else "unresolved"
        return {
            "native_refs": refs, "resolution_state": state,
            "canonical_agent_session_id": next(iter(resolved)) if len(resolved) == 1 else None,
            "resolution_evidence": evidence,
        }

    def prompts(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Reverse-chrono cross-project prompt list.

        Filter chips supported (passed as params):
          - persona: filter to specific persona slug
          - project_slug: filter to specific project
          - q: FTS5 search term applied to content_preview
          - include_synthetic: include hook/task/system payloads for diagnostics
          - limit, offset
        """
        limit = min(int(params.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
        offset = int(params.get("offset", 0))
        include_synthetic = str(params.get("include_synthetic", "")).lower() in {
            "1", "true", "yes", "on",
        }
        persona = params.get("persona")
        project = params.get("project_slug")
        runtime = params.get("runtime")
        source_kind = params.get("source_kind")
        q = (params.get("q") or "").strip()

        if not self.index_db.exists():
            return []

        con = _open_ro(self.index_db)
        try:
            where_clauses = ["type = 'UserPromptSubmit'"]
            if not include_synthetic:
                where_clauses.append("COALESCE(is_synthetic, 0) = 0")
            args: list[Any] = []
            if persona:
                where_clauses.append("persona = ?")
                args.append(persona)
            if project:
                where_clauses.append("project_slug = ?")
                args.append(project)
            if runtime:
                where_clauses.append("runtime = ?")
                args.append(runtime)
            if source_kind:
                where_clauses.append("source_kind = ?")
                args.append(source_kind)

            # FTS5 path if q present
            if q:
                # Same FTS5 token-quoting as in search() — neutralize hyphens
                tokens = q.split()
                fts_query = " ".join('"' + t.replace('"', '""') + '"' for t in tokens) if tokens else q
                cursor = con.execute(
                    f"SELECT ei.event_id, ei.project_slug, ei.session_id, ei.type, ei.ts, "
                    f"ei.persona, ei.content_preview, ei.has_full_content, ei.runtime, ei.source_kind "
                    f"FROM events_index_fts fts "
                    f"JOIN events_index ei ON ei.event_id = fts.event_id "
                    f"WHERE fts.events_index_fts MATCH ? AND {' AND '.join(where_clauses)} "
                    f"ORDER BY ei.ts DESC LIMIT ? OFFSET ?",
                    (fts_query, *args, limit, offset),
                )
            else:
                cursor = con.execute(
                    f"SELECT event_id, project_slug, session_id, type, ts, "
                    f"persona, content_preview, has_full_content, runtime, source_kind "
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
            "runtime": row["runtime"],
            "source_kind": row["source_kind"],
        }

    def sessions(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Rich cross-project transcript catalog, newest activity first."""
        generated_titles = read_titles(self.title_db)
        limit = min(int(params.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
        offset = int(params.get("offset", 0))
        project = params.get("project_slug")
        runtime = params.get("runtime")
        source_kind = params.get("source_kind")

        include_native = runtime not in ("claude-web",) and source_kind not in ("archive",)
        include_web = self.claude_web_enabled and runtime in (None, "", "claude-web") and source_kind in (None, "", "archive") and project in (None, "", "claude-web")

        # Fetch enough from each independently sorted source, then merge and
        # apply pagination once over the unified chronology.
        # Each source must contribute through the requested global offset;
        # limiting this to one response page silently truncates later pages.
        fetch_count = min(limit + offset, 100_000)
        web_sessions = list_claude_web_sessions(fetch_count, 0) if include_web else []
        for item in web_sessions:
            item["synopsis"] = [
                {"type": "UserPromptSubmit", "content": item.get("opening_prompt") or item.get("title") or "", "label": "Opening prompt"},
                {"type": "AssistantResponse", "content": item.get("description") or "", "label": "Latest agent response"},
            ]
            generated = generated_titles.get(item.get("session_id", ""))
            if generated:
                item["title"] = generated["title"]
                item["description"] = generated.get("description") or item.get("description") or ""
                item["title_model"] = generated["model"]
                item["title_generated_at"] = generated["generated_at"]

        if not self.index_db.exists() or not include_native:
            return web_sessions[offset:offset + limit]

        con = _open_ro(self.index_db)
        try:
            filters = []
            args: list[Any] = []
            if project:
                filters.append("project_slug = ?")
                args.append(project)
            if runtime:
                filters.append("runtime = ?")
                args.append(runtime)
            if source_kind:
                filters.append("source_kind = ?")
                args.append(source_kind)
            where = "WHERE " + " AND ".join(filters) if filters else ""
            cursor = con.execute(
                f"WITH catalog AS ("
                f" SELECT session_id, MIN(ts) AS started_at, MAX(ts) AS updated_at, "
                f" COUNT(*) AS event_count, GROUP_CONCAT(DISTINCT project_slug) AS project_slugs, "
                f" GROUP_CONCAT(DISTINCT runtime) AS runtimes, "
                f" GROUP_CONCAT(DISTINCT source_kind) AS source_kinds "
                f" FROM events_index {where} GROUP BY session_id"
                f") SELECT c.*, "
                f" (SELECT content_preview FROM events_index p WHERE p.session_id=c.session_id "
                f"  AND p.type='UserPromptSubmit' "
                f"  AND COALESCE(p.is_synthetic,0)=0 ORDER BY p.ts ASC LIMIT 1) AS opening_prompt, "
                f" (SELECT content_preview FROM events_index a WHERE a.session_id=c.session_id "
                f"  AND a.type='AssistantResponse' "
                f"  ORDER BY a.ts ASC LIMIT 1) AS description, "
                f" (SELECT content_preview FROM events_index lp WHERE lp.session_id=c.session_id "
                f"  AND lp.type='UserPromptSubmit' AND COALESCE(lp.is_synthetic,0)=0 "
                f"  ORDER BY lp.ts DESC LIMIT 1) AS last_prompt, "
                f" (SELECT content_preview FROM events_index lr WHERE lr.session_id=c.session_id "
                f"  AND lr.type='AssistantResponse' ORDER BY lr.ts DESC LIMIT 1) AS last_response, "
                f" (SELECT persona FROM events_index pe WHERE pe.session_id=c.session_id "
                f"  AND pe.persona IS NOT NULL "
                f"  ORDER BY pe.ts ASC LIMIT 1) AS persona "
                f"FROM catalog c ORDER BY c.updated_at DESC LIMIT ? OFFSET ?",
                (*args, fetch_count, 0),
            )
            results = []
            for row in cursor:
                item = dict(row)
                project_slugs = (item.get("project_slugs") or "").split(",")
                item["project_slugs"] = project_slugs
                item["runtimes"] = (item.get("runtimes") or "claude").split(",")
                item["source_kinds"] = (item.get("source_kinds") or "live").split(",")
                item["project_slug"] = project_slugs[0] if project_slugs else ""
                item["identity"] = self._session_identity(
                    item["session_id"], item["runtimes"], project_slugs,
                )
                opening = item.get("opening_prompt") or ""
                opening_line = (opening.splitlines() or [""])[0]
                generated = generated_titles.get(item["session_id"])
                item["title"] = generated["title"] if generated else (opening_line[:100] or "Untitled session")
                item["title_model"] = generated["model"] if generated else None
                item["title_generated_at"] = generated["generated_at"] if generated else None
                item["description"] = (generated or {}).get("description") or item.get("description") or ""
                item["synopsis"] = [
                    {"type": "UserPromptSubmit", "content": item.get("opening_prompt") or "", "label": "Opening prompt"},
                    {"type": "AssistantResponse", "content": item.get("last_response") or "", "label": "Latest agent response"},
                    {"type": "UserPromptSubmit", "content": item.get("last_prompt") or "", "label": "Latest prompt"},
                ]
                item["latest_message_at"] = item["updated_at"]
                item["tags"] = [item["persona"]] if item.get("persona") else []
                results.append(item)
            combined = results + web_sessions
            combined.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
            return combined[offset:offset + limit]
        finally:
            con.close()

    def transcript(self, params: dict[str, Any]) -> dict[str, Any]:
        """Single-session transcript. Modes: 'clean' (prompts + responses only)
        or 'full' (all events)."""
        session_id = params.get("session", params.get("session_id"))
        requested_project = params.get("project_slug")
        mode = params.get("mode", "clean")
        if not session_id:
            return {"error": "session_id required"}
        web_transcript = get_claude_web_transcript(session_id, mode) if self.claude_web_enabled else None
        if web_transcript is not None:
            editorial = read_titles(self.title_db).get(session_id)
            if editorial:
                web_transcript["title"] = editorial["title"]
                web_transcript["description"] = editorial.get("description") or web_transcript.get("description") or ""
                web_transcript["title_model"] = editorial["model"]
                web_transcript["title_generated_at"] = editorial["generated_at"]
            return web_transcript
        if not self.index_db.exists():
            return {"error": "index DB missing"}

        # A single Claude session can cross working directories, which causes
        # hook events to land in multiple project shards. Resolve and merge all
        # of them; project_slug remains an optional validation hint for links.
        idx_con = _open_ro(self.index_db)
        try:
            summary = idx_con.execute(
                "SELECT MIN(ts) AS started_at, MAX(ts) AS updated_at, "
                "COUNT(*) AS total_event_count, "
                "GROUP_CONCAT(DISTINCT runtime) AS runtimes, "
                "GROUP_CONCAT(DISTINCT source_kind) AS source_kinds, "
                "GROUP_CONCAT(DISTINCT model) AS models, "
                "MAX(CASE WHEN persona IS NOT NULL THEN persona END) AS persona, "
                "(SELECT content_preview FROM events_index p "
                " WHERE p.session_id = ? AND p.type='UserPromptSubmit' "
                " AND COALESCE(p.is_synthetic,0)=0 ORDER BY p.ts ASC LIMIT 1) AS opening_prompt "
                "FROM events_index WHERE session_id = ?",
                (session_id, session_id),
            ).fetchone()
            project_rows = idx_con.execute(
                "SELECT project_slug, MIN(ts) AS first_ts FROM events_index "
                "WHERE session_id = ? GROUP BY project_slug ORDER BY first_ts ASC",
                (session_id,),
            ).fetchall()
            project_slugs = [row["project_slug"] for row in project_rows]
            if not project_slugs:
                return {"error": f"session not found: {session_id}"}
            if requested_project and requested_project not in project_slugs:
                return {"error": f"session {session_id} not found in project {requested_project}"}
        finally:
            idx_con.close()

        if mode == "clean":
            type_filter = "AND type IN ('UserPromptSubmit', 'AssistantResponse')"
        else:
            type_filter = ""

        events_by_id: dict[str, dict[str, Any]] = {}
        for slug in project_slugs:
            proj_db = self.root / slug / "db" / "logging.db"
            if not proj_db.exists():
                continue
            proj_con = _open_ro(proj_db)
            try:
                columns = {row[1] for row in proj_con.execute("PRAGMA table_info(events)")}
                provenance = [
                    name if name in columns else f"NULL AS {name}"
                    for name in (
                        "runtime", "runtime_event", "turn_id", "capture_source",
                        "source_kind", "model", "permission_mode", "duration_ms",
                    )
                ]
                cursor = proj_con.execute(
                    f"SELECT id, type, ts, persona, agent_id, tool_name, content, data, "
                    f"{', '.join(provenance)} "
                    f"FROM events WHERE session_id = ? {type_filter} ORDER BY ts ASC",
                    (session_id,),
                )
                for row in cursor:
                    try:
                        data = json.loads(row["data"]) if row["data"] else {}
                    except json.JSONDecodeError:
                        data = {}
                    tool_call_id = (
                        data.get("tool_use_id") or data.get("tool_call_id")
                        or data.get("call_id")
                    )
                    tool_status = None
                    if row["type"] == "PreToolUse":
                        tool_status = "started"
                    elif row["type"] in ("PostToolUse", "ToolResult"):
                        tool_status = "completed"
                    elif row["type"] in ("PostToolUseFailure", "ToolError"):
                        tool_status = "failed"
                    content = row["content"]
                    content_truncated = False
                    if mode == "explore" and content and len(content) > 2000:
                        content = content[:2000]
                        content_truncated = True
                    events_by_id[row["id"]] = {
                        "event_id": row["id"], "type": row["type"], "ts": row["ts"],
                        "persona": row["persona"], "agent_id": row["agent_id"],
                        "tool_name": row["tool_name"], "content": content,
                        "content_truncated": content_truncated,
                        "runtime": row["runtime"] or "claude",
                        "runtime_event": row["runtime_event"] or row["type"],
                        "turn_id": row["turn_id"], "capture_source": row["capture_source"],
                        "source_kind": row["source_kind"] or "live",
                        "model": row["model"], "permission_mode": row["permission_mode"],
                        "tool_call_id": tool_call_id, "tool_status": tool_status,
                        "duration_ms": row["duration_ms"],
                        "data": None if mode == "explore" else data,
                        "data_loaded": mode != "explore", "project_slug": slug,
                    }
            finally:
                proj_con.close()
        events = sorted(events_by_id.values(), key=lambda event: event["ts"])
        native_title, latest_message, native_project = _catalog_native_metadata(session_id)
        generated_title = read_titles(self.title_db).get(session_id)
        opening_prompt = summary["opening_prompt"] if summary else ""
        title = (generated_title or {}).get("title") or native_title or ((opening_prompt or "").splitlines() or [""])[0][:100]
        result = {
            "session_id": session_id,
            "project_slug": native_project or project_slugs[0],
            "project_slugs": project_slugs,
            "mode": mode,
            "event_count": len(events),
            "total_event_count": summary["total_event_count"] if summary else len(events),
            "started_at": summary["started_at"] if summary else None,
            "updated_at": latest_message or (summary["updated_at"] if summary else None),
            "runtimes": (summary["runtimes"] or "claude").split(",") if summary else ["claude"],
            "source_kinds": (summary["source_kinds"] or "live").split(",") if summary else ["live"],
            "models": (summary["models"] or "").split(",") if summary else [],
            "persona": summary["persona"] if summary else None,
            "opening_prompt": opening_prompt,
            "title": title or "Untitled session",
            "description": (generated_title or {}).get("description") or "",
            "title_model": (generated_title or {}).get("model"),
            "title_generated_at": (generated_title or {}).get("generated_at"),
            "events": events,
        }
        graph_runtime = next((runtime for runtime in result["runtimes"] if runtime in {"pi", "prime-agent", "omp", "hermes"}), None)
        if graph_runtime:
            graph = get_pi_session_graph(session_id, graph_runtime)
            if graph:
                result["session_graph"] = graph
                if graph.get("title"):
                    result["title"] = graph["title"]
        return result

    def event_detail(self, params: dict[str, Any]) -> dict[str, Any]:
        """Return one complete event payload for the session inspector."""
        event_id = params.get("event", params.get("event_id"))
        session_id = params.get("session", params.get("session_id"))
        if not event_id or not session_id:
            return {"error": "session_id and event_id required"}
        transcript = self.transcript({"session": session_id, "mode": "full"})
        if transcript.get("error"):
            return transcript
        for event in transcript["events"]:
            if event["event_id"] == event_id:
                return event
        return {"error": f"event not found: {event_id}"}

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
            projects = [dict(row) for row in cursor]
            web = list_claude_web_sessions(100_000, 0) if self.claude_web_enabled else []
            if web:
                projects.append({
                    "project_slug": "claude-web",
                    "event_count": sum(int(item.get("event_count") or 0) for item in web),
                    "last_event_ts": max((item.get("updated_at") or "" for item in web), default=""),
                })
                projects.sort(key=lambda item: item.get("last_event_ts") or "", reverse=True)
            return projects
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
        include_synthetic = str(params.get("include_synthetic", "")).lower() in {
            "1", "true", "yes", "on",
        }
        runtime = params.get("runtime")
        source_kind = params.get("source_kind")

        include_native = runtime not in ("claude-web",) and source_kind not in ("archive",)
        include_web = self.claude_web_enabled and runtime in (None, "", "claude-web") and source_kind in (None, "", "archive")

        if not q:
            return {"mode": mode, "q": "", "results": []}
        web_results = search_claude_web_sessions(q, mode, limit + offset, 0) if include_web and mode in ("prompts", "events") else []
        if not self.index_db.exists() or not include_native:
            results = web_results[offset:offset + limit]
            return {"mode": mode, "q": q, "results": results, "count": len(results)}

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
                if not include_synthetic:
                    type_filter += " AND COALESCE(ei.is_synthetic, 0) = 0"
            elif mode == "events":
                type_filter = ""
            elif mode == "semantic":
                return {"mode": mode, "q": q, "results": [], "error": "semantic search Phase 5+"}
            else:
                type_filter = ""
            if runtime:
                type_filter += " AND ei.runtime = ?"
            if source_kind:
                type_filter += " AND ei.source_kind = ?"

            filter_args = []
            if runtime:
                filter_args.append(runtime)
            if source_kind:
                filter_args.append(source_kind)

            cursor = con.execute(
                f"SELECT ei.event_id, ei.project_slug, ei.session_id, ei.type, ei.ts, "
                f"ei.persona, ei.content_preview, ei.has_full_content, ei.runtime, ei.source_kind "
                f"FROM events_index_fts fts "
                f"JOIN events_index ei ON ei.event_id = fts.event_id "
                f"WHERE fts.events_index_fts MATCH ? {type_filter} "
                f"ORDER BY ei.ts DESC LIMIT ? OFFSET ?",
                (fts_query, *filter_args, limit + offset, 0),
            )
            results = [self._prompt_row(row) for row in cursor] + web_results
            results.sort(key=lambda item: item.get("ts") or "", reverse=True)
            results = results[offset:offset + limit]
            return {"mode": mode, "q": q, "results": results, "count": len(results)}
        finally:
            con.close()

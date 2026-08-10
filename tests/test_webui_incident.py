"""Regression coverage for INC-20260806-001."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from lib.token_meter import is_synthetic
from web.logging_accessor import LoggingAccessor, _sqlite_utc_timestamp
from lib.session_titles import open_title_db


def _index(tmp_path: Path) -> Path:
    root = tmp_path / "logging"
    index_dir = root / "_index"
    index_dir.mkdir(parents=True)
    db = index_dir / "index.db"
    schema = Path(__file__).parents[1] / "scripts/v2/init_cross_project_index.sql"
    con = sqlite3.connect(db)
    con.executescript(schema.read_text())
    con.executemany(
        "INSERT INTO events_index "
        "(event_id, project_slug, session_id, type, ts, persona, content_preview, "
        "has_full_content, is_synthetic) VALUES (?, 'project', 'session', "
        "'UserPromptSubmit', ?, 'shawn', ?, 0, ?)",
        [
            ("human", "2026-08-06T20:00:00Z", "Please fix the logging UI", 0),
            (
                "task",
                "2026-08-06T20:01:00Z",
                "<task-notification><task-id>abc</task-id></task-notification>",
                1,
            ),
        ],
    )
    con.commit()
    con.close()
    return root


def test_task_notifications_are_synthetic() -> None:
    assert is_synthetic("<task-notification><task-id>abc</task-id></task-notification>")
    assert not is_synthetic("Please investigate task notifications")


def test_prompt_feed_excludes_synthetic_by_default(tmp_path: Path) -> None:
    accessor = LoggingAccessor(_index(tmp_path))
    assert [row["event_id"] for row in accessor.prompts({})] == ["human"]


def test_prompt_feed_can_include_synthetic_for_diagnostics(tmp_path: Path) -> None:
    accessor = LoggingAccessor(_index(tmp_path))
    rows = accessor.prompts({"include_synthetic": "1"})
    assert [row["event_id"] for row in rows] == ["task", "human"]


def test_prompt_search_excludes_synthetic_by_default(tmp_path: Path) -> None:
    root = _index(tmp_path)
    con = sqlite3.connect(root / "_index/index.db")
    con.executemany(
        "INSERT INTO events_index_fts "
        "(event_id, project_slug, session_id, type, persona, content_preview) "
        "VALUES (?, 'project', 'session', 'UserPromptSubmit', 'shawn', ?)",
        [
            ("human", "Please fix the logging UI"),
            ("task", "<task-notification>logging UI task</task-notification>"),
        ],
    )
    con.commit()
    con.close()

    accessor = LoggingAccessor(root)
    assert [row["event_id"] for row in accessor.search({"q": "logging", "mode": "prompts"})["results"]] == ["human"]
    included = accessor.search(
        {"q": "logging", "mode": "prompts", "include_synthetic": "true"}
    )["results"]
    assert [row["event_id"] for row in included] == ["task", "human"]


def test_populated_prompt_list_removes_empty_container_style() -> None:
    html = (Path(__file__).parents[1] / "web/static/index.html").read_text()
    assert "list.classList.remove('empty')" in html


def test_sessions_are_primary_navigation_and_use_rows() -> None:
    html = (Path(__file__).parents[1] / "web/static/index.html").read_text()
    sessions_pos = html.index('data-tab="sessions"')
    prompts_pos = html.index('data-tab="prompts"')
    assert sessions_pos < prompts_pos
    assert "localPath === '/sessions'" in html
    assert "window.addEventListener('popstate'" in html
    assert "history.pushState(historyState, '', url)" in html
    assert "renderSessionTable(visible)" in html
    assert "grid grid-cols-1 xl:grid-cols-2" not in html
    assert "function openSession(" in html
    assert "function backToSessions()" in html
    assert "function renderRoute({useSessionCache = false} = {})" in html
    assert "renderRoute({useSessionCache: state.tab === 'sessions' && !state.currentSession && state.sessions.length > 0})" in html
    assert "renderRoute({useSessionCache: tab === 'sessions' && state.sessions.length > 0})" in html
    assert "class=\"session-shell\"" in html
    assert "class=\"session-document\"" in html
    assert "CONVERSATION" in html
    assert "MESSAGE METADATA" in html
    assert "EVENT INSPECTOR" not in html
    assert "grid-template-columns: 220px minmax(0, 1fr)" in html
    assert "grid-template-columns: 220px minmax(0, 1fr) 300px" not in html
    assert "function messageUrl(" in html
    assert "function openMessageDetail(" in html
    assert "function backToTranscript()" in html
    assert "const messageMatch = localPath.match" in html
    assert "const mapEvents = events.filter(e => ['UserPromptSubmit','AssistantResponse'].includes(e.type))" in html
    assert "function renderConversationMapLink(e)" in html
    assert "function wireConversationMinimap(root)" in html
    assert "function clearSessionSelection(t, updateUrl = true)" in html
    assert "function toggleSessionSelection(t, eventId, scroll = true)" in html
    assert "if (state.selectedEventId === eventId) clearSessionSelection(t)" in html
    assert "toggleSessionSelection(t, element.dataset.sessionEvent, false)" in html
    assert 'data-message-detail="${escapeHtml(event.event_id)}"' in html
    assert "event.stopPropagation()" in html
    assert "event.key !== 'Escape'" in html
    assert "history.state?.selectedEventId || ''" in html
    assert "events[0].event_id, false, false" not in html
    assert "new IntersectionObserver(entries =>" in html
    assert "link.classList.toggle('in-viewport', visible.has(eventId))" in html
    assert "entry.target.classList.toggle('in-viewport', entry.isIntersecting)" in html
    assert "wireConversationMinimap(main)" in html
    assert ".session-map-link.in-viewport" in html
    assert ".session-message.in-viewport:not(.selected)" in html
    assert ".session-message:hover:not(.selected)" in html
    assert ".session-map-link.active{background:#cba6f719" in html
    assert ".timeline-event.selected { border-color:#cba6f7" in html
    assert "class=\"session-breadcrumbs session-page-breadcrumbs\"" in html
    assert 'data-session-event="${escapeHtml(event.event_id)}"' in html
    assert "Copy link" not in html
    assert "← Older" not in html
    assert "Newer →" not in html
    assert '<code>${escapeHtml(t.session_id)}</code>' in html
    assert "mode: state.transcriptMode === 'raw' ? 'full' : 'explore'" in html
    assert "sessionDetailsVisible: false" in html
    assert 'data-action="toggle-session-details"' in html
    assert "state.sessionDetailsVisible = !state.sessionDetailsVisible" in html
    assert "function renderInlineEventDetails(event)" in html
    assert "function renderRawPayloadShell(event)" in html
    assert "function jsonSyntaxHtml(value)" in html
    assert "data-raw-payload" in html
    assert "details.dataset.rendered === 'true'" in html
    assert ".raw-payload-body { max-height:65vh; overflow:auto" in html
    assert '<pre class="inspector-json mt-3">' not in html
    assert 'data-mode="detailed"' not in html
    assert 'id="session-query"' in html
    assert 'id="session-type-filter"' in html
    assert "function sessionEventUrl(" in html
    assert "#event-${encodeURIComponent(eventId)}" in html
    assert "state.sessionScrollY = window.scrollY" in html
    assert "state.routeGeneration += 1" in html
    assert "if (!routeIsCurrent()) return" in html
    assert "route: 'sessions', tab: 'sessions', catalogScrollY" in html
    assert "event.state?.route === 'sessions'" in html
    assert "state.liveRefresh = true" in html
    assert "function startLivePush()" in html
    assert "new BroadcastChannel(`legion.logging.live:${_liveScope}`)" in html
    assert "if (_liveChannel && !_liveLeader) return" in html
    assert "_liveChannel?.postMessage({type: 'corpus-changed'})" in html
    assert "function applySessionFilterVisibility(t, root)" in html
    assert "applySessionFilterVisibility(t, main)" in html
    assert "_sessionFilterDebounce" not in html
    assert "useTranscriptCache = false" in html
    assert "state.currentTranscriptData?.session_id === routeSession" in html
    assert "renderTranscriptTab({useTranscriptCache: true})" in html
    assert "function userIsEditing()" in html
    assert "_liveRefreshPending = true" in html
    assert "if (_liveRefreshPending && !userIsEditing()) scheduleLiveRefetch()" in html
    assert "async function refreshCurrentSessionIncrementally()" in html
    assert "timeline.insertAdjacentHTML('beforeend'" in html
    assert "map.insertAdjacentHTML('beforeend'" in html
    assert "? refreshCurrentSessionIncrementally" in html
    assert "wireTranscriptInteractions(main, next)" in html
    assert "function toolStatusBadge(e)" in html
    assert "e.tool_call_id" in html
    assert "function renderSessionGraph(graph)" in html
    assert "showing ${visibleIndexes.length.toLocaleString()} of ${totalNodes.toLocaleString()} nodes" in html
    assert "data-lineage-session" in html


def test_session_catalog_paints_first_page_and_loads_incrementally() -> None:
    html = (Path(__file__).parents[1] / "web/static/index.html").read_text()
    assert "state.sessionView === 'cards' ? 12 : 100" in html
    assert "offset = append ? state.sessions.length : 0" in html
    assert "data-session-view=\"cards\"" in html
    assert "data-session-view=\"table\"" in html
    assert "const messages = (t.synopsis || [])" in html
    assert "hydrateSessionCards(visible)" not in html
    assert "session-card-transcript" in html
    assert "function markdownHtml(source)" in html
    assert "function firstCompleteSentence(source)" in html
    assert "function sessionSynopsis(events)" in html
    assert "Opening prompt" in html
    assert "Latest agent response" in html
    assert "Latest prompt" in html
    assert "content:'▶'; color:#89b4fa" in html
    assert "content:'◆'; color:#a6e3a1" in html
    assert "event.type === 'AssistantResponse' ? 'assistant' : 'user'" in html
    assert "session-reader-scroll" in html
    assert ".session-reader-scroll { min-height:60vh; overflow:visible;" in html
    assert "height:min(72vh,820px); overflow:auto" not in html
    assert "if (!state.sessions.length) main.innerHTML" in html
    assert "const sessions = useCache ? state.sessions : await loadSessionCatalog()" in html
    assert "for (let offset" not in html


def test_generated_session_title_overrides_prompt_title(tmp_path: Path) -> None:
    root = _index(tmp_path)
    con = open_title_db(root / "_index/session-metadata.db")
    con.execute(
        "INSERT INTO session_titles(session_id,title,model,prompt_version,source_hash) VALUES(?,?,?,?,?)",
        ("session", "The Green Archive Awakens", "google/gemma-4-31b-it", "v1", "abc"),
    )
    con.commit()
    con.close()
    card = LoggingAccessor(root, claude_web_enabled=False).sessions({"limit": "1"})[0]
    assert card["title"] == "The Green Archive Awakens"
    assert card["title_model"] == "google/gemma-4-31b-it"


def test_pi_transcript_attaches_native_session_graph(tmp_path: Path, monkeypatch) -> None:
    root = _index(tmp_path)
    con = sqlite3.connect(root / "_index/index.db")
    con.execute("UPDATE events_index SET runtime = 'pi'")
    con.commit()
    con.close()
    graph = {"runtime": "pi", "version": 3, "title": "named pi session", "nodes": [], "leaves": []}
    monkeypatch.setattr("web.logging_accessor.get_pi_session_graph", lambda _session_id, _runtime: graph)

    transcript = LoggingAccessor(root).transcript({"session": "session", "mode": "explore"})
    assert transcript["session_graph"] == graph
    assert transcript["title"] == "named pi session"


def test_session_catalog_has_composite_lookup_index(tmp_path: Path) -> None:
    root = _index(tmp_path)
    con = sqlite3.connect(root / "_index/index.db")
    indexes = {row[1] for row in con.execute("PRAGMA index_list(events_index)")}
    con.close()
    assert "idx_events_index_session_type_ts" in indexes


def test_adapter_health_returns_a_runtime_map(tmp_path: Path) -> None:
    accessor = LoggingAccessor(_index(tmp_path))
    con = sqlite3.connect(root := accessor.index_db)
    con.row_factory = sqlite3.Row
    try:
        health = accessor._adapter_health(con)
    finally:
        con.close()
    assert {"claude", "codex", "claude-web", "pi", "prime-agent", "omp"} <= set(health)


def test_healthz_returns_adapter_health(tmp_path: Path) -> None:
    health = LoggingAccessor(_index(tmp_path)).healthz()
    assert isinstance(health, dict)
    assert "adapters" in health["stats"]
    assert "prime-agent" in health["stats"]["adapters"]


def test_sqlite_current_timestamp_is_interpreted_as_utc() -> None:
    now_utc = sqlite3.connect(":memory:").execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
    assert abs(time.time() - _sqlite_utc_timestamp(now_utc)) < 2


def test_rollup_discovery_skips_token_meter_only_database(tmp_path: Path, monkeypatch) -> None:
    from scripts.v2 import rollup_index

    meter_db = tmp_path / "meter-only" / "db" / "logging.db"
    meter_db.parent.mkdir(parents=True)
    con = sqlite3.connect(meter_db)
    con.execute("CREATE TABLE prompts (prompt_id TEXT PRIMARY KEY)")
    con.commit()
    con.close()

    event_db = tmp_path / "event-store" / "db" / "logging.db"
    event_db.parent.mkdir(parents=True)
    con = sqlite3.connect(event_db)
    con.execute("CREATE TABLE events (id TEXT PRIMARY KEY)")
    con.commit()
    con.close()

    monkeypatch.setattr(rollup_index, "LOGGING_ROOT", tmp_path)
    assert rollup_index.discover_dbs() == [("event-store", event_db)]


def test_rollup_replaces_fts_mirror_instead_of_duplicating() -> None:
    source = (Path(__file__).parents[1] / "scripts/v2/rollup_index.py").read_text()
    delete = source.index('DELETE FROM events_index_fts WHERE event_id = ?')
    insert = source.index('INSERT OR REPLACE INTO events_index_fts')
    assert delete < insert


def test_session_catalog_has_row_metadata(tmp_path: Path) -> None:
    accessor = LoggingAccessor(_index(tmp_path))
    cards = accessor.sessions({})
    assert len(cards) == 1
    card = cards[0]
    assert card["session_id"] == "session"
    assert card["project_slug"] == "project"
    assert card["project_slugs"] == ["project"]
    assert card["title"] == "Please fix the logging UI"
    assert card["opening_prompt"] == "Please fix the logging UI"
    assert card["started_at"]
    assert card["latest_message_at"]
    assert card["tags"] == ["shawn"]


def test_session_catalog_handles_session_without_human_prompt(tmp_path: Path) -> None:
    root = _index(tmp_path)
    con = sqlite3.connect(root / "_index/index.db")
    con.execute(
        "INSERT INTO events_index "
        "(event_id, project_slug, session_id, type, ts, content_preview, "
        "has_full_content, is_synthetic) VALUES "
        "('startup', 'project', 'startup-only', 'SessionStart', "
        "'2026-08-06T19:00:00Z', 'Session started', 0, 0)"
    )
    con.commit()
    con.close()
    cards = LoggingAccessor(root).sessions({})
    startup = next(card for card in cards if card["session_id"] == "startup-only")
    assert startup["title"] == "Untitled session"
    assert startup["opening_prompt"] is None

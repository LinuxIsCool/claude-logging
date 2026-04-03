"""API endpoint tests for claude-logging REST server."""

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


@pytest.fixture
def client(tmp_storage, monkeypatch):
    """TestClient with monkeypatched storage pointing at tmp fixtures."""
    monkeypatch.setattr("api.server.storage", tmp_storage)
    monkeypatch.setattr("api.server.STORAGE_PATH", tmp_storage.base_path)
    monkeypatch.setattr("api.server._search_service", None)
    monkeypatch.setattr("api.server._search_has_semantic", False)

    from starlette.testclient import TestClient
    from api.server import app
    return TestClient(app, raise_server_exceptions=False)


class TestAPIRoot:
    def test_root(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestAPISearch:
    def test_search_basic(self, client):
        r = client.post("/api/search", json={"query": "authentication"})
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 1
        assert any("authentication" in res["content"].lower() for res in data["results"])

    def test_search_empty_query(self, client):
        r = client.post("/api/search", json={"query": ""})
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_search_no_results(self, client):
        r = client.post("/api/search", json={"query": "xyznonexistent123"})
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_search_event_type_filter(self, client):
        r = client.post("/api/search", json={
            "query": "auth",
            "event_types": ["PreToolUse"],
        })
        assert r.status_code == 200
        for res in r.json()["results"]:
            assert res["event_type"] == "PreToolUse"

    def test_search_date_filter(self, client):
        r = client.post("/api/search", json={
            "query": "database",
            "date_from": "2026-04-02",
            "date_to": "2026-04-02T23:59:59",
        })
        assert r.status_code == 200
        for res in r.json()["results"]:
            assert "2026-04-02" in res["timestamp"]


class TestAPISessions:
    def test_list_sessions(self, client):
        r = client.get("/api/sessions")
        assert r.status_code == 200
        sessions = r.json()
        assert len(sessions) == 3

    def test_list_sessions_pagination(self, client):
        r = client.get("/api/sessions?limit=1&offset=0")
        assert r.status_code == 200
        assert len(r.json()) == 1

    def test_get_session_detail(self, client):
        r = client.get("/api/sessions/sess-beta")
        assert r.status_code == 200
        data = r.json()
        assert data["session"]["id"] == "sess-beta"
        assert len(data["events"]) >= 7

    def test_get_session_404(self, client):
        r = client.get("/api/sessions/nonexistent-session")
        assert r.status_code == 404


class TestAPIStats:
    def test_stats(self, client):
        r = client.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["session_count"] == 3
        assert data["event_count"] >= 16


class TestAPIRecentEvents:
    def test_recent_events(self, client):
        r = client.get("/api/events/recent?limit=5")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] <= 5
        assert len(data["results"]) <= 5

    def test_recent_events_type_filter(self, client):
        r = client.get("/api/events/recent?event_types=UserPromptSubmit")
        assert r.status_code == 200
        for res in r.json()["results"]:
            assert res["event_type"] == "UserPromptSubmit"


class TestAPISecurity:
    def test_image_path_traversal_session(self, client):
        """Path traversal attempts must not return 200 with file contents."""
        r = client.get("/api/images/..%2F..%2Fetc/passwd")
        assert r.status_code in (400, 403, 404, 422)

    def test_image_invalid_filename(self, client):
        """Invalid filenames must not serve arbitrary files."""
        r = client.get("/api/images/sess-alpha/..%2F..%2Fetc%2Fshadow")
        assert r.status_code in (400, 403, 404, 422)


class TestAPISync:
    def test_sync(self, client):
        r = client.get("/api/sync")
        assert r.status_code == 200
        assert "synced" in r.json()

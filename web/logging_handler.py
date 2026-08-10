"""LoggingHandler — WebuiHandler subclass with extra GET routes.

Adds /api/prompts, /api/sessions, /api/transcript, /api/projects, /api/search
to the kernel's standard routes (/api/list, /api/detail, /api/stats,
/api/feed, /healthz, /static/*).

task-508 Phase 2.3. Pattern follows claude-backlog/server.py BacklogHandler.
"""
from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

from claude_webui import WebuiKernel
from claude_webui.kernel import WebuiHandler


class LoggingHandler(WebuiHandler):
    """WebuiHandler + logging-specific extra GET routes.

    Override `_dispatch_get` so the kernel's standard routes (/api/list,
    /api/stats, /healthz, /static/*) work unchanged AND the satellite-
    specific routes resolve before the kernel's not-found fallback.
    """

    def _dispatch_get(self) -> None:  # noqa: D401
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        params_raw = parse_qs(parsed.query, keep_blank_values=True)
        params = {k: v[0] if v else "" for k, v in params_raw.items()}

        # Session details are first-class browser locations. Serve the SPA
        # shell for both the catalog and individual permalinks; client-side
        # routing resolves the session id without sacrificing reloadability.
        if path == "/sessions" or path.startswith("/sessions/"):
            self._serve_index()
            return

        if path == "/api/prompts":
            try:
                payload = self.accessor.prompts(params)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=500)
                return
            self._send_json(payload)
            return

        if path == "/api/sessions":
            try:
                payload = self.accessor.sessions(params)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=500)
                return
            self._send_json(payload)
            return

        if path == "/api/transcript":
            try:
                payload = self.accessor.transcript(params)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=500)
                return
            self._send_json(payload)
            return

        if path == "/api/event":
            try:
                payload = self.accessor.event_detail(params)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=500)
                return
            self._send_json(payload)
            return

        if path == "/api/projects":
            try:
                payload = self.accessor.projects(params)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=500)
                return
            self._send_json(payload)
            return

        if path == "/api/personas":
            try:
                payload = self.accessor.personas()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=500)
                return
            self._send_json(payload)
            return

        if path == "/api/search":
            try:
                payload = self.accessor.search(params)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=500)
                return
            self._send_json(payload)
            return

        # Fallback to kernel's standard dispatch (handles /api/list,
        # /api/detail/<id>, /api/stats, /api/feed, /healthz, /static/<path>,
        # /index.html, /, etc.)
        super()._dispatch_get()


class LoggingKernel(WebuiKernel):
    """WebuiKernel that builds a LoggingHandler subclass.

    Identical to the upstream kernel except for the handler class used —
    keeps every other concern (server lifecycle, build_server, serve, stop,
    bind warnings) untouched.
    """

    def _make_handler_class(self) -> type[WebuiHandler]:
        accessor = self.accessor
        static_dir = self.static_dir
        event_bus = self._event_bus
        mutation_catalog = self._mutation_catalog

        class _Handler(LoggingHandler):
            pass

        _Handler.accessor = accessor  # type: ignore[attr-defined]
        _Handler.static_dir = static_dir  # type: ignore[attr-defined]
        _Handler.event_bus = event_bus  # type: ignore[attr-defined]
        _Handler.mutation_catalog = mutation_catalog  # type: ignore[attr-defined]
        return _Handler

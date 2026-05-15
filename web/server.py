"""claude-logging web server — thin satellite of claude_webui.WebuiKernel.

task-508 Phase 2 — kernel-webui migration. Replaces the Next.js renderer
(now quarantined in web-legacy/).

Mode A (standalone): python web/server.py --port 8870
Mode B (mounted):    via claude-webui Platform on /logging/

Architecture:
  1. LoggingAccessor wires the data shape over per-project + cross-project DBs
  2. LoggingHandler subclasses WebuiHandler for satellite-specific GET routes
  3. static/index.html is the vanilla JS SPA (Catppuccin Mocha cluster aesthetic)
  4. Hub-bar via embedded-bar.js (Layer 2 static dispatch from claude-webui)

If this file grows past ~120 LOC, push it into the accessor (data shape) or
upstream into claude-webui (cross-cutting shell concern). Pattern from
claude-backlog/web/server.py.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# When run as `python web/server.py`, the plugin's lib/ + web/ are NOT on
# sys.path. Inject:
#   1. The plugin root so `from web.logging_accessor import ...` resolves.
#   2. claude-webui sibling so `from claude_webui import ...` resolves.
# (Mode B factory does this differently — see webui_platform.py — but Mode A
# standalone needs both.)
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_PLUGINS_DIR = _PLUGIN_ROOT.parent  # legion-plugins/plugins/
_CLAUDE_WEBUI = _PLUGINS_DIR / "claude-webui"
for p in (_PLUGIN_ROOT, _CLAUDE_WEBUI):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from claude_webui import WebuiKernel  # noqa: E402

from web.logging_accessor import NAMESPACE, LoggingAccessor  # noqa: E402
from web.logging_handler import LoggingKernel  # noqa: E402

STATIC_DIR: Path = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 8870

# Plugin version — bumped to 2.0.0-pre after task-508 Phase 2 ships
_LOGGING_VERSION = "2.0.0-pre"


def build_kernel(
    *,
    port: int = DEFAULT_PORT,
    bind: str = "127.0.0.1",
    logging_root: Path | None = None,
) -> WebuiKernel:
    """Construct a configured kernel for the claude-logging satellite.

    Returns the kernel without calling `.serve()` so tests can drive the
    underlying server directly via `kernel.build_server()`.

    No MutationCatalog wired in Phase 2 — logging is read-only at this
    stage. Phase 8 (annotations) adds the write surface via
    register_mutation_handlers().
    """
    accessor = LoggingAccessor(logging_root=logging_root)
    kernel = LoggingKernel(
        accessor=accessor,
        port=port,
        bind=bind,
        static_dir=STATIC_DIR,
    )
    kernel.satellite_namespace = NAMESPACE  # type: ignore[attr-defined]
    kernel.satellite_version = _LOGGING_VERSION  # type: ignore[attr-defined]
    return kernel


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--no-open", action="store_true",
                        help="Do not xdg-open the URL on startup")
    args = parser.parse_args()

    kernel = build_kernel(port=args.port, bind=args.bind)
    url = f"http://{args.bind}:{args.port}/"
    print(f"claude-logging webui — Mode A standalone", file=sys.stderr)
    print(f"  namespace: {NAMESPACE}", file=sys.stderr)
    print(f"  version:   {_LOGGING_VERSION}", file=sys.stderr)
    print(f"  bind:      {args.bind}:{args.port}", file=sys.stderr)
    print(f"  url:       {url}", file=sys.stderr)
    if args.bind != "127.0.0.1":
        print(f"  WARNING: bind != 127.0.0.1 — webui exposed beyond localhost", file=sys.stderr)

    if not args.no_open:
        try:
            import subprocess
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    kernel.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())

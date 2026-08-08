"""Import point for `legion_capture`, the shared capture-boundary library.

One shared library, imported here once, so the rule it enforces can only be
broken in one place. See `packages/legion-capture/` in the legion-plugins
superproject.

**No silent fallback.** If the library cannot be imported, `CAPTURE_AVAILABLE`
is False and `IMPORT_ERROR` says why, and the caller must decide loudly. The
alternative -- quietly reverting to the old guess-based path -- is precisely
the failure mode this library exists to end: a capability regresses, nothing
errors, and the data is wrong for months. `extract_images_from_prompt` was
lost exactly that way and left 2,501 orphaned files behind.

Resolution order:
  1. a normal import, for when the package is installed
  2. the superproject checkout, for this machine
"""

from __future__ import annotations

import sys
from pathlib import Path

CAPTURE_AVAILABLE = False
IMPORT_ERROR: str | None = None

try:
    import legion_capture  # noqa: F401
    CAPTURE_AVAILABLE = True
except ImportError:
    # plugins/claude-logging/lib/ -> plugins/ -> <superproject>/
    _superproject = Path(__file__).resolve().parents[3]
    _src = _superproject / "packages" / "legion-capture" / "src"
    if _src.is_dir():
        sys.path.insert(0, str(_src))
        try:
            import legion_capture  # noqa: F401
            CAPTURE_AVAILABLE = True
        except ImportError as e:      # pragma: no cover
            IMPORT_ERROR = f"found {_src} but import failed: {e}"
    else:
        IMPORT_ERROR = (
            f"legion_capture is not installed and {_src} does not exist. "
            f"Install it: uv pip install -e packages/legion-capture"
        )

if CAPTURE_AVAILABLE:
    from legion_capture import (  # noqa: F401
        SUBMIT_KINDS, CaptureError, Discriminator, Kind, decode, guard,
        is_submit, is_valid, matches_ts, stamp, uuid7,
    )


def require() -> None:
    """Raise unless the library is present. Call before any capture write."""
    if not CAPTURE_AVAILABLE:
        raise RuntimeError(
            f"legion_capture unavailable, refusing to write unstamped rows: "
            f"{IMPORT_ERROR}"
        )

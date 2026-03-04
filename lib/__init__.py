"""
Logging Plugin Core Library

This module provides core functionality for the logging plugin:
- Storage: JSONL and SQLite storage backends
- Search: Hybrid FTS5 + semantic search
- Embeddings: Local embedding generation
"""

from pathlib import Path
import os

__version__ = "1.0.0"


def get_storage_path() -> Path:
    """Get the storage path for logging data."""
    storage_path = os.environ.get("LOGGING_STORAGE_PATH")
    if storage_path:
        return Path(storage_path)

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    return Path(project_dir) / ".claude" / "local" / "logging"


def get_plugin_root() -> Path:
    """Get the plugin root directory."""
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        return Path(plugin_root)

    # Fallback: relative to this file
    return Path(__file__).parent.parent

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


def encode_project_path(project_dir: str) -> str:
    """Encode a project directory path for use as a directory name.

    Mirrors Claude Code's ~/.claude/projects/ convention:
    /home/shawn/Workspace/app -> -home-shawn-Workspace-app
    """
    return project_dir.replace("/", "-")


def get_storage_path() -> Path:
    """Get the centralized storage path for logging data.

    Logs are stored at ~/.claude/local/logging/<encoded-project-path>/
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    encoded = encode_project_path(project_dir)
    return Path.home() / ".claude" / "local" / "logging" / encoded


def get_plugin_root() -> Path:
    """Get the plugin root directory."""
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        return Path(plugin_root)

    # Fallback: relative to this file
    return Path(__file__).parent.parent

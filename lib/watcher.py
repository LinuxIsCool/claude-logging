"""
Claude Directory Watcher

Monitors ~/.claude/ directories for changes using watchfiles.
Detects new sessions, transcript updates, and subagent creation.
"""

import asyncio
import re
from pathlib import Path
from typing import Optional, Callable, List, Set, Dict, Any
from dataclasses import dataclass
from enum import Enum


class ChangeType(Enum):
    """Types of file system changes."""
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"


@dataclass
class FileChange:
    """Represents a detected file change."""
    change_type: ChangeType
    path: Path
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    is_subagent: bool = False
    project_path: Optional[str] = None


class ClaudeWatcher:
    """
    Watches ~/.claude/projects/ for transcript changes.

    Uses watchfiles for efficient file system monitoring.
    Parses paths to extract session IDs and agent IDs.
    """

    # Pattern for session UUID (standard v4 format)
    SESSION_PATTERN = re.compile(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
        re.IGNORECASE
    )

    # Pattern for subagent files
    SUBAGENT_PATTERN = re.compile(r'^agent-([0-9a-f]+)\.jsonl$', re.IGNORECASE)

    def __init__(
        self,
        claude_dir: Optional[Path] = None,
        callback: Optional[Callable[[FileChange], None]] = None
    ):
        """
        Initialize the watcher.

        Args:
            claude_dir: Path to ~/.claude/ directory (defaults to ~/.claude)
            callback: Function to call when changes detected
        """
        self.claude_dir = claude_dir or Path.home() / ".claude"
        self.projects_dir = self.claude_dir / "projects"
        self.instances_dir = self.claude_dir / "instances"
        self.callback = callback

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._known_sessions: Set[str] = set()

    @property
    def is_running(self) -> bool:
        """Check if watcher is currently running."""
        return self._running

    async def start(self) -> None:
        """Start watching for file changes."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._watch_loop())

    async def stop(self) -> None:
        """Stop watching for file changes."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def discover_sessions(self) -> List[Dict[str, Any]]:
        """
        Discover all existing sessions in ~/.claude/projects/.

        Returns list of dicts with session info:
        - session_id: UUID
        - transcript_path: Path to main JSONL
        - project_path: Encoded project path
        - subagents: List of subagent paths
        - size: File size in bytes
        """
        sessions = []

        if not self.projects_dir.exists():
            return sessions

        for project_dir in self.projects_dir.iterdir():
            if not project_dir.is_dir():
                continue

            project_path = project_dir.name

            # Look for session directories or direct JSONL files
            for item in project_dir.iterdir():
                if item.is_file() and item.suffix == ".jsonl":
                    # Direct session JSONL (old format)
                    session_id = item.stem
                    if self.SESSION_PATTERN.match(session_id):
                        sessions.append({
                            "session_id": session_id,
                            "transcript_path": item,
                            "project_path": project_path,
                            "subagents": [],
                            "size": item.stat().st_size,
                        })

                elif item.is_dir() and self.SESSION_PATTERN.match(item.name):
                    # Session directory (new format)
                    session_id = item.name
                    transcript = item / f"{session_id}.jsonl"

                    if not transcript.exists():
                        # Try looking for any JSONL in the directory
                        jsonls = list(item.glob("*.jsonl"))
                        if jsonls:
                            transcript = jsonls[0]
                        else:
                            continue

                    # Find subagents
                    subagents = []
                    subagents_dir = item / "subagents"
                    if subagents_dir.exists():
                        for sa_file in subagents_dir.glob("agent-*.jsonl"):
                            match = self.SUBAGENT_PATTERN.match(sa_file.name)
                            if match:
                                subagents.append({
                                    "agent_id": match.group(1),
                                    "path": sa_file,
                                    "size": sa_file.stat().st_size,
                                })

                    sessions.append({
                        "session_id": session_id,
                        "transcript_path": transcript,
                        "project_path": project_path,
                        "subagents": subagents,
                        "size": transcript.stat().st_size if transcript.exists() else 0,
                    })

        return sessions

    def parse_path(self, path: Path) -> Optional[FileChange]:
        """
        Parse a file path to extract session and agent information.

        Args:
            path: Absolute path to a file

        Returns:
            FileChange with extracted info, or None if not a relevant file
        """
        # Only care about JSONL files
        if path.suffix != ".jsonl":
            return None

        try:
            # Make path relative to projects dir
            rel_path = path.relative_to(self.projects_dir)
            parts = rel_path.parts
        except ValueError:
            # Not under projects dir
            return None

        if len(parts) < 2:
            return None

        project_path = parts[0]

        # Check for subagent pattern: project/session/subagents/agent-xxx.jsonl
        if len(parts) >= 4 and parts[2] == "subagents":
            session_id = parts[1]
            if not self.SESSION_PATTERN.match(session_id):
                return None

            match = self.SUBAGENT_PATTERN.match(parts[3])
            if match:
                return FileChange(
                    change_type=ChangeType.MODIFIED,
                    path=path,
                    session_id=session_id,
                    agent_id=match.group(1),
                    is_subagent=True,
                    project_path=project_path,
                )

        # Check for direct session file: project/session-id.jsonl
        elif len(parts) == 2:
            session_id = path.stem
            if self.SESSION_PATTERN.match(session_id):
                return FileChange(
                    change_type=ChangeType.MODIFIED,
                    path=path,
                    session_id=session_id,
                    project_path=project_path,
                )

        # Check for session directory format: project/session/session.jsonl
        elif len(parts) >= 2:
            session_id = parts[1]
            if self.SESSION_PATTERN.match(session_id):
                return FileChange(
                    change_type=ChangeType.MODIFIED,
                    path=path,
                    session_id=session_id,
                    project_path=project_path,
                )

        return None

    async def _watch_loop(self) -> None:
        """Main watch loop using watchfiles."""
        try:
            import watchfiles

            paths_to_watch = [self.projects_dir]
            if self.instances_dir.exists():
                paths_to_watch.append(self.instances_dir)

            async for changes in watchfiles.awatch(*paths_to_watch):
                if not self._running:
                    break

                for change_type, path_str in changes:
                    path = Path(path_str)

                    # Parse the path
                    file_change = self.parse_path(path)
                    if not file_change:
                        continue

                    # Set change type
                    if change_type == watchfiles.Change.added:
                        file_change.change_type = ChangeType.ADDED
                    elif change_type == watchfiles.Change.modified:
                        file_change.change_type = ChangeType.MODIFIED
                    elif change_type == watchfiles.Change.deleted:
                        file_change.change_type = ChangeType.DELETED

                    # Invoke callback
                    if self.callback:
                        try:
                            self.callback(file_change)
                        except Exception:
                            pass  # Don't let callback errors stop the watcher

        except ImportError:
            # Fallback to polling if watchfiles not available
            await self._poll_loop()

    async def _poll_loop(self) -> None:
        """Fallback polling loop when watchfiles is not available."""
        seen_sizes: Dict[str, int] = {}

        while self._running:
            try:
                for session_info in self.discover_sessions():
                    path = session_info["transcript_path"]
                    key = str(path)
                    current_size = session_info["size"]
                    last_size = seen_sizes.get(key, 0)

                    if current_size > last_size:
                        # File grew - trigger callback
                        file_change = FileChange(
                            change_type=ChangeType.MODIFIED if last_size > 0 else ChangeType.ADDED,
                            path=path,
                            session_id=session_info["session_id"],
                            project_path=session_info["project_path"],
                        )

                        if self.callback:
                            try:
                                self.callback(file_change)
                            except Exception:
                                pass

                        seen_sizes[key] = current_size

                    # Check subagents
                    for sa in session_info.get("subagents", []):
                        sa_key = str(sa["path"])
                        sa_size = sa["size"]
                        sa_last = seen_sizes.get(sa_key, 0)

                        if sa_size > sa_last:
                            sa_change = FileChange(
                                change_type=ChangeType.MODIFIED if sa_last > 0 else ChangeType.ADDED,
                                path=sa["path"],
                                session_id=session_info["session_id"],
                                agent_id=sa["agent_id"],
                                is_subagent=True,
                                project_path=session_info["project_path"],
                            )

                            if self.callback:
                                try:
                                    self.callback(sa_change)
                                except Exception:
                                    pass

                            seen_sizes[sa_key] = sa_size

            except Exception:
                pass

            await asyncio.sleep(1)  # Poll every second

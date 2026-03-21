#!/usr/bin/env python3
"""
Unit tests for heartbeat monitoring — Phase 1 silent failure detection.

Tests heartbeat writing, staleness detection, and the health directory setup.
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest"]
# ///

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

# We need to import these functions from log_event.py
# But it has side effects (argparse), so we import the module and extract
import importlib.util

spec = importlib.util.spec_from_file_location(
    "log_event",
    str(Path(__file__).resolve().parent.parent / "hooks" / "log_event.py"),
)
log_event_mod = importlib.util.module_from_spec(spec)
# Patch sys.argv before exec to prevent argparse from firing
original_argv = sys.argv
sys.argv = ["log_event.py", "-e", "Stop"]
spec.loader.exec_module(log_event_mod)
sys.argv = original_argv

write_heartbeat = log_event_mod.write_heartbeat
check_stale_heartbeats = log_event_mod.check_stale_heartbeats
HEARTBEAT_MAX_AGE_SECONDS = log_event_mod.HEARTBEAT_MAX_AGE_SECONDS


class TestWriteHeartbeat:

    def test_creates_heartbeat_file(self, tmp_path):
        with patch.object(log_event_mod, "HEALTH_DIR", tmp_path):
            write_heartbeat("logging")
            hb = tmp_path / "logging-heartbeat"
            assert hb.exists()
            content = hb.read_text().strip()
            # Should be an ISO timestamp
            assert "T" in content

    def test_creates_health_dir(self, tmp_path):
        health = tmp_path / "subdir" / "health"
        with patch.object(log_event_mod, "HEALTH_DIR", health):
            write_heartbeat("test")
            assert health.exists()
            assert (health / "test-heartbeat").exists()

    def test_overwrites_existing(self, tmp_path):
        with patch.object(log_event_mod, "HEALTH_DIR", tmp_path):
            write_heartbeat("logging")
            first = (tmp_path / "logging-heartbeat").read_text()
            time.sleep(0.01)
            write_heartbeat("logging")
            second = (tmp_path / "logging-heartbeat").read_text()
            assert first != second  # timestamps differ

    def test_never_raises(self, tmp_path):
        """Heartbeat writes must never raise, even on permission errors."""
        with patch.object(log_event_mod, "HEALTH_DIR", Path("/nonexistent/path")):
            # Should silently fail, not raise
            write_heartbeat("test")


class TestCheckStaleHeartbeats:

    def test_no_health_dir(self, tmp_path):
        nonexistent = tmp_path / "nope"
        with patch.object(log_event_mod, "HEALTH_DIR", nonexistent):
            assert check_stale_heartbeats() == []

    def test_fresh_heartbeat_not_stale(self, tmp_path):
        with patch.object(log_event_mod, "HEALTH_DIR", tmp_path):
            write_heartbeat("logging")
            stale = check_stale_heartbeats()
            assert len(stale) == 0

    def test_old_heartbeat_detected(self, tmp_path):
        with patch.object(log_event_mod, "HEALTH_DIR", tmp_path):
            hb = tmp_path / "logging-heartbeat"
            hb.write_text("old\n")
            # Set mtime to 48 hours ago
            old_time = time.time() - 48 * 3600
            os.utime(hb, (old_time, old_time))

            stale = check_stale_heartbeats()
            assert len(stale) == 1
            assert stale[0][0] == "logging"
            assert stale[0][1] > 24  # age in hours

    def test_missing_heartbeat_not_flagged(self, tmp_path):
        """A heartbeat that was never written is not stale — it's not yet active."""
        with patch.object(log_event_mod, "HEALTH_DIR", tmp_path):
            tmp_path.mkdir(exist_ok=True)
            stale = check_stale_heartbeats()
            assert len(stale) == 0

    def test_mixed_fresh_and_stale(self, tmp_path):
        with patch.object(log_event_mod, "HEALTH_DIR", tmp_path):
            # Fresh
            write_heartbeat("logging")
            # Stale
            hb = tmp_path / "embedding-heartbeat"
            hb.write_text("old\n")
            os.utime(hb, (time.time() - 48 * 3600, time.time() - 48 * 3600))

            stale = check_stale_heartbeats()
            stale_names = [s[0] for s in stale]
            assert "embedding" in stale_names
            assert "logging" not in stale_names


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

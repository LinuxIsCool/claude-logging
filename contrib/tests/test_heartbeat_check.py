#!/usr/bin/env python3
"""
Tests for heartbeat_check.py — standalone pipeline health monitor.

Tests config loading, per-pipeline threshold checking, KOI freshness
(mocked), scratchpad alerting, and output formatting.
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "pyyaml"]
# ///

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# Import the module under test
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "contrib"))
import heartbeat_check as hbc

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def health_dir(tmp_path):
    """Create a temporary health directory."""
    d = tmp_path / "health"
    d.mkdir()
    return d


@pytest.fixture
def config_file(tmp_path):
    """Create a temporary config file."""
    cfg = tmp_path / "heartbeats.yml"
    cfg.write_text(
        yaml.dump(
            {
                "pipelines": {
                    "logging": {"warn_hours": 2, "description": "Test logging"},
                    "dreams": {"warn_hours": 168, "description": "Test dreams"},
                    "fast": {"warn_hours": 1, "description": "Fast pipeline"},
                },
                "critical_multiplier": 2,
                "scratchpad_dir": str(tmp_path / "scratchpad"),
            }
        )
    )
    return cfg


@pytest.fixture
def default_config():
    """Return a config dict matching DEFAULT_CONFIG structure."""
    return {
        "pipelines": {
            "logging": {"warn_hours": 2, "description": "Test logging"},
            "dreams": {"warn_hours": 168, "description": "Test dreams"},
            "fast": {"warn_hours": 1, "description": "Fast pipeline"},
        },
        "critical_multiplier": 2,
        "scratchpad_dir": "~/.claude/local/scratchpad",
    }


def write_hb(health_dir: Path, name: str, age_hours: float = 0):
    """Write a heartbeat file with a given age."""
    hb = health_dir / f"{name}-heartbeat"
    ts = datetime.now(timezone.utc)
    hb.write_text(f"{ts.isoformat()}\n")
    if age_hours > 0:
        old_time = time.time() - (age_hours * 3600)
        os.utime(hb, (old_time, old_time))
    return hb


# ── TestLoadConfig ────────────────────────────────────────────────────


class TestLoadConfig:
    def test_loads_yaml_config(self, config_file):
        config = hbc.load_config(config_file)
        assert "logging" in config["pipelines"]
        assert config["pipelines"]["logging"]["warn_hours"] == 2
        assert config["critical_multiplier"] == 2

    def test_fallback_defaults_when_missing(self, tmp_path):
        missing = tmp_path / "nonexistent.yml"
        config = hbc.load_config(missing)
        assert "logging" in config["pipelines"]
        assert config["pipelines"]["logging"]["warn_hours"] == 2
        assert "dreams" in config["pipelines"]

    def test_fallback_on_invalid_yaml(self, tmp_path):
        bad = tmp_path / "bad.yml"
        bad.write_text("{{{{not yaml")
        config = hbc.load_config(bad)
        # Should fall back to defaults, not crash
        assert "logging" in config["pipelines"]


# ── TestCheckPipeline ─────────────────────────────────────────────────


class TestCheckPipeline:
    def test_fresh_heartbeat(self, health_dir, default_config):
        write_hb(health_dir, "logging", age_hours=0)
        result = hbc.check_pipeline("logging", default_config, health_dir)
        assert result["status"] == "fresh"
        assert result["name"] == "logging"
        assert result["age_hours"] is not None
        assert result["age_hours"] < 1

    def test_stale_heartbeat(self, health_dir, default_config):
        # logging threshold is 2h, write 3h old → stale (not critical since < 4h)
        write_hb(health_dir, "logging", age_hours=3)
        result = hbc.check_pipeline("logging", default_config, health_dir)
        assert result["status"] == "stale"
        assert result["threshold_hours"] == 2

    def test_critical_heartbeat(self, health_dir, default_config):
        # logging threshold 2h, critical at 4h. Write 5h old → critical
        write_hb(health_dir, "logging", age_hours=5)
        result = hbc.check_pipeline("logging", default_config, health_dir)
        assert result["status"] == "critical"
        assert result["critical_hours"] == 4

    def test_missing_heartbeat(self, health_dir, default_config):
        result = hbc.check_pipeline("logging", default_config, health_dir)
        assert result["status"] == "missing"
        assert result["age_hours"] is None
        assert result["last_seen"] is None

    def test_per_pipeline_thresholds(self, health_dir, default_config):
        # dreams has 168h threshold — 48h old should be fresh
        write_hb(health_dir, "dreams", age_hours=48)
        result = hbc.check_pipeline("dreams", default_config, health_dir)
        assert result["status"] == "fresh"
        assert result["threshold_hours"] == 168

        # fast has 1h threshold — 1.5h old should be stale
        write_hb(health_dir, "fast", age_hours=1.5)
        result = hbc.check_pipeline("fast", default_config, health_dir)
        assert result["status"] == "stale"

    def test_result_structure(self, health_dir, default_config):
        write_hb(health_dir, "logging")
        result = hbc.check_pipeline("logging", default_config, health_dir)
        expected_keys = {"name", "status", "age_hours", "threshold_hours", "critical_hours", "last_seen", "description"}
        assert set(result.keys()) == expected_keys


# ── TestCheckKoiFreshness ─────────────────────────────────────────────


class TestCheckKoiFreshness:
    def _mock_psycopg2(self):
        """Create a mock psycopg2 module for patching the local import."""
        return patch.dict("sys.modules", {"psycopg2": MagicMock()})

    def test_queries_and_writes_heartbeat(self, health_dir):
        config = {
            "pipelines": {"koi": {"warn_hours": 24, "description": "KOI"}},
            "critical_multiplier": 2,
        }
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        fresh_ts = datetime.now(timezone.utc)
        mock_cursor.fetchone.return_value = (fresh_ts,)

        with self._mock_psycopg2():
            import psycopg2 as mock_pg

            mock_pg.connect = MagicMock(return_value=mock_conn)
            result = hbc.check_koi_freshness(config, health_dir)

        assert result["name"] == "koi"
        assert result["status"] == "fresh"
        assert (health_dir / "koi-heartbeat").exists()

    def test_handles_db_unavailable(self, health_dir):
        config = {
            "pipelines": {"koi": {"warn_hours": 24, "description": "KOI"}},
            "critical_multiplier": 2,
        }
        with self._mock_psycopg2():
            import psycopg2 as mock_pg

            mock_pg.connect = MagicMock(side_effect=Exception("Connection refused"))
            result = hbc.check_koi_freshness(config, health_dir)

        # Falls back to file check → missing
        assert result["status"] == "missing"

    def test_handles_empty_table(self, health_dir):
        config = {
            "pipelines": {"koi": {"warn_hours": 24, "description": "KOI"}},
            "critical_multiplier": 2,
        }
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (None,)

        with self._mock_psycopg2():
            import psycopg2 as mock_pg

            mock_pg.connect = MagicMock(return_value=mock_conn)
            result = hbc.check_koi_freshness(config, health_dir)

        assert result["status"] == "missing"


# ── TestScratchpadAlert ───────────────────────────────────────────────


class TestScratchpadAlert:
    def test_writes_alert_for_critical_pipelines(self, tmp_path):
        critical = [
            {"name": "logging", "age_hours": 10, "threshold_hours": 2, "status": "critical", "description": "Test"},
        ]
        path = hbc.write_scratchpad_alert(critical, tmp_path)
        assert path is not None
        assert path.exists()
        content = path.read_text()
        assert "pipeline-health" in content
        assert "logging" in content
        assert "10h stale" in content

    def test_no_alert_when_no_critical(self, tmp_path):
        path = hbc.write_scratchpad_alert([], tmp_path)
        assert path is None

    def test_alert_frontmatter_format(self, tmp_path):
        critical = [
            {"name": "test", "age_hours": 5, "threshold_hours": 2, "status": "critical", "description": "Test"},
        ]
        path = hbc.write_scratchpad_alert(critical, tmp_path)
        content = path.read_text()
        assert content.startswith("---\n")
        assert "harvested: false" in content
        assert "tags:" in content

    def test_alert_filename_format(self, tmp_path):
        critical = [
            {"name": "test", "age_hours": 5, "threshold_hours": 2, "status": "critical", "description": "Test"},
        ]
        path = hbc.write_scratchpad_alert(critical, tmp_path)
        assert "pipeline-health-alert.md" in path.name
        # Should have ISO-ish timestamp prefix
        assert path.name[4] == "-"  # YYYY-


# ── TestCheckAll ──────────────────────────────────────────────────────


class TestCheckAll:
    def test_returns_all_pipelines(self, health_dir):
        config = {
            "pipelines": {
                "a": {"warn_hours": 1, "description": "A"},
                "b": {"warn_hours": 2, "description": "B"},
            },
            "critical_multiplier": 2,
        }
        write_hb(health_dir, "a")
        write_hb(health_dir, "b")
        results = hbc.check_all(config, health_dir)
        names = {r["name"] for r in results}
        assert names == {"a", "b"}

    def test_sorted_by_severity(self, health_dir):
        config = {
            "pipelines": {
                "fresh_one": {"warn_hours": 100, "description": "Fresh"},
                "stale_one": {"warn_hours": 1, "description": "Stale"},
            },
            "critical_multiplier": 2,
        }
        write_hb(health_dir, "fresh_one")
        write_hb(health_dir, "stale_one", age_hours=1.5)
        results = hbc.check_all(config, health_dir)
        assert results[0]["name"] == "stale_one"
        assert results[1]["name"] == "fresh_one"


# ── TestFormatHuman ───────────────────────────────────────────────────


class TestFormatHuman:
    def test_human_output_contains_pipelines(self):
        results = [
            {
                "name": "logging",
                "status": "fresh",
                "age_hours": 0.5,
                "threshold_hours": 2,
                "critical_hours": 4,
                "last_seen": "ts",
                "description": "Test",
            },
            {
                "name": "hippo",
                "status": "stale",
                "age_hours": 50,
                "threshold_hours": 48,
                "critical_hours": 96,
                "last_seen": "ts",
                "description": "Test",
            },
        ]
        output = hbc.format_human(results)
        assert "logging" in output
        assert "hippo" in output
        assert "🟢" in output
        assert "🟡" in output
        assert "Summary:" in output


# ── TestBackwardCompatibility ─────────────────────────────────────────


class TestBackwardCompatibility:
    """Ensure the existing functions in log_event.py still work."""

    def test_existing_write_heartbeat(self, tmp_path):
        """Verify the log_event.py write_heartbeat function still works."""
        # Import from log_event
        spec = __import__("importlib").util.spec_from_file_location(
            "log_event_compat",
            str(Path(__file__).resolve().parent.parent / "hooks" / "log_event.py"),
        )
        mod = __import__("importlib").util.module_from_spec(spec)
        original_argv = sys.argv
        sys.argv = ["log_event.py", "-e", "Stop"]
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.argv = original_argv

        with patch.object(mod, "HEALTH_DIR", tmp_path):
            mod.write_heartbeat("test")
            assert (tmp_path / "test-heartbeat").exists()

    def test_existing_check_stale_heartbeats(self, tmp_path):
        """Verify the log_event.py check_stale_heartbeats still finds koi and journal."""
        spec = __import__("importlib").util.spec_from_file_location(
            "log_event_compat2",
            str(Path(__file__).resolve().parent.parent / "hooks" / "log_event.py"),
        )
        mod = __import__("importlib").util.module_from_spec(spec)
        original_argv = sys.argv
        sys.argv = ["log_event.py", "-e", "Stop"]
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.argv = original_argv

        # koi and journal should be in HEARTBEAT_NAMES
        assert "koi" in mod.HEARTBEAT_NAMES
        assert "journal" in mod.HEARTBEAT_NAMES


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

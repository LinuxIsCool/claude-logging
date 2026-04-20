#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "psycopg2-binary"]
# ///
"""
Pipeline heartbeat monitor — detect silent failures across logging pipelines.

Checks heartbeat files in ~/.claude/local/health/ against per-pipeline
staleness thresholds defined in heartbeats.yml. Outputs structured health
status with severity levels (fresh/stale/critical/missing).

Origin: Dream Crystal #2 — "Silent failures are louder than crashes"
        (task-047, dream-promoted from cycle 1545e75e)

Usage:
    uv run heartbeat_check.py              # human-readable table
    uv run heartbeat_check.py --json       # JSON array output
    uv run heartbeat_check.py --quiet      # exit code only (0/1/2)

Exit codes:
    0 — all pipelines fresh
    1 — at least one pipeline stale
    2 — at least one pipeline critical (2x threshold)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ── Constants ─────────────────────────────────────────────────────────

HEALTH_DIR = Path.home() / ".claude" / "local" / "health"
CONFIG_PATH = HEALTH_DIR / "heartbeats.yml"
KOI_DSN = os.environ.get("KOI_DSN", "")  # Optional: set to enable KOI freshness check

SEVERITY_ORDER = {"critical": 0, "stale": 1, "missing": 2, "fresh": 3}

# Fallback defaults if heartbeats.yml is missing or incomplete
DEFAULT_CONFIG = {
    "pipelines": {
        "logging": {"warn_hours": 2, "description": "Claude Code event logging"},
        "embedding": {"warn_hours": 24, "description": "Semantic embedding"},
        "rhythm": {"warn_hours": 14, "description": "Daily brief generation"},
        "hippo": {"warn_hours": 48, "description": "FalkorDB knowledge graph"},
        "koi": {"warn_hours": 24, "description": "KOI bundle ingestion"},
        "journal": {"warn_hours": 24, "description": "Journal entry creation"},
        "dreams": {"warn_hours": 168, "description": "Dream cycle completion"},
    },
    "critical_multiplier": 2,
    "scratchpad_dir": "~/.claude/local/scratchpad",
}


# ── Config ────────────────────────────────────────────────────────────


def load_config(config_path: Path = CONFIG_PATH) -> dict:
    """Load pipeline thresholds from YAML config, with fallback defaults."""
    config = DEFAULT_CONFIG.copy()
    try:
        if config_path.exists():
            with open(config_path) as f:
                loaded = yaml.safe_load(f)
            if loaded and isinstance(loaded, dict):
                if "pipelines" in loaded:
                    config["pipelines"] = loaded["pipelines"]
                if "critical_multiplier" in loaded:
                    config["critical_multiplier"] = loaded["critical_multiplier"]
                if "scratchpad_dir" in loaded:
                    config["scratchpad_dir"] = loaded["scratchpad_dir"]
    except Exception as e:
        print(f"Warning: failed to load config from {config_path}: {e}", file=sys.stderr)
    return config


# ── Pipeline Checks ───────────────────────────────────────────────────


def check_pipeline(
    name: str,
    config: dict,
    health_dir: Path = HEALTH_DIR,
) -> dict:
    """Check a single pipeline's heartbeat file against its threshold.

    Returns a dict with: name, status, age_hours, threshold_hours,
    critical_hours, last_seen, description.
    """
    pipeline_config = config["pipelines"].get(name, {})
    warn_hours = pipeline_config.get("warn_hours", 24)
    critical_multiplier = config.get("critical_multiplier", 2)
    critical_hours = warn_hours * critical_multiplier
    description = pipeline_config.get("description", name)

    heartbeat_path = health_dir / f"{name}-heartbeat"

    if not heartbeat_path.exists():
        return {
            "name": name,
            "status": "missing",
            "age_hours": None,
            "threshold_hours": warn_hours,
            "critical_hours": critical_hours,
            "last_seen": None,
            "description": description,
        }

    try:
        mtime = heartbeat_path.stat().st_mtime
        now = datetime.now(timezone.utc).timestamp()
        age_seconds = now - mtime
        age_hours = round(age_seconds / 3600, 1)

        # Try to read the ISO timestamp from file content
        content = heartbeat_path.read_text().strip()
        last_seen = content if "T" in content else None

        if age_hours >= critical_hours:
            status = "critical"
        elif age_hours >= warn_hours:
            status = "stale"
        else:
            status = "fresh"

        return {
            "name": name,
            "status": status,
            "age_hours": age_hours,
            "threshold_hours": warn_hours,
            "critical_hours": critical_hours,
            "last_seen": last_seen,
            "description": description,
        }
    except Exception as e:
        print(f"Warning: error checking {name} heartbeat: {e}", file=sys.stderr)
        return {
            "name": name,
            "status": "missing",
            "age_hours": None,
            "threshold_hours": warn_hours,
            "critical_hours": critical_hours,
            "last_seen": None,
            "description": description,
        }


def check_koi_freshness(
    config: dict,
    health_dir: Path = HEALTH_DIR,
) -> dict:
    """Check KOI pipeline by querying a PostgreSQL database for latest bundle timestamp.

    Writes koi-heartbeat as a side effect so mtime-based health checks
    still pick it up.
    """
    pipeline_config = config["pipelines"].get("koi", {})
    warn_hours = pipeline_config.get("warn_hours", 24)
    critical_multiplier = config.get("critical_multiplier", 2)
    critical_hours = warn_hours * critical_multiplier
    description = pipeline_config.get("description", "KOI bundle ingestion")

    try:
        import psycopg2

        conn = psycopg2.connect(KOI_DSN, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT MAX(updated_at) FROM bundles")
        row = cur.fetchone()
        cur.close()
        conn.close()

        if row and row[0]:
            last_ts = row[0]
            # Ensure timezone-aware
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            age_hours = round((now - last_ts).total_seconds() / 3600, 1)
            last_seen = last_ts.isoformat()

            # Write koi-heartbeat as side effect
            try:
                health_dir.mkdir(parents=True, exist_ok=True)
                hb_path = health_dir / "koi-heartbeat"
                hb_path.write_text(f"{last_seen}\n")
            except OSError:
                pass

            if age_hours >= critical_hours:
                status = "critical"
            elif age_hours >= warn_hours:
                status = "stale"
            else:
                status = "fresh"

            return {
                "name": "koi",
                "status": status,
                "age_hours": age_hours,
                "threshold_hours": warn_hours,
                "critical_hours": critical_hours,
                "last_seen": last_seen,
                "description": description,
            }
        else:
            return {
                "name": "koi",
                "status": "missing",
                "age_hours": None,
                "threshold_hours": warn_hours,
                "critical_hours": critical_hours,
                "last_seen": None,
                "description": description,
            }
    except Exception as e:
        # Fall back to heartbeat file if DB is unreachable
        result = check_pipeline("koi", config, health_dir)
        if result["status"] == "missing":
            print(f"Warning: KOI DB unreachable ({e}), no heartbeat file found", file=sys.stderr)
        return result


def check_all(
    config: dict,
    health_dir: Path = HEALTH_DIR,
) -> list[dict]:
    """Check all pipelines and return results sorted by severity."""
    results = []
    for name in config["pipelines"]:
        if name == "koi":
            results.append(check_koi_freshness(config, health_dir))
        else:
            results.append(check_pipeline(name, config, health_dir))

    results.sort(key=lambda r: SEVERITY_ORDER.get(r["status"], 99))
    return results


# ── Scratchpad Alerting ───────────────────────────────────────────────


def write_scratchpad_alert(
    critical_pipelines: list[dict],
    scratchpad_dir: Path,
) -> Path | None:
    """Write a scratchpad thought when pipelines reach critical threshold.

    Returns the path of the created file, or None if nothing was written.
    """
    if not critical_pipelines:
        return None

    try:
        scratchpad_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        filename = f"{now.strftime('%Y-%m-%dT%H-%M-%S')}-pipeline-health-alert.md"
        filepath = scratchpad_dir / filename

        lines = []
        for p in critical_pipelines:
            age = p["age_hours"] or "unknown"
            threshold = p["threshold_hours"]
            lines.append(f"- **{p['name']}**: {age}h stale (threshold: {threshold}h)")

        content = f"""---
created: {now.isoformat()}
tags:
  - pipeline-health
  - alert
  - heartbeat
  - dream-promoted
harvested: false
---

Pipeline health alert — {len(critical_pipelines)} critical pipeline(s):

{chr(10).join(lines)}

These pipelines have exceeded 2x their staleness threshold. Investigate immediately.
Source: heartbeat_check.py (task-047, dream crystal #2)
"""
        filepath.write_text(content)
        return filepath
    except Exception as e:
        print(f"Warning: failed to write scratchpad alert: {e}", file=sys.stderr)
        return None


# ── Output Formatting ─────────────────────────────────────────────────

def _load_status_emoji() -> dict[str, str]:
    """Load health emoji from emoji_flat.json. Falls back to defaults."""
    _DEFAULTS = {"fresh": "🟢", "stale": "🟡", "critical": "🔴", "missing": "⚪"}
    try:
        import json as _json
        _flat = _json.loads(Path("~/.claude/local/emoji/emoji_flat.json").expanduser().read_text())
        return {
            "fresh": _flat.get("health:healthy", _DEFAULTS["fresh"]),
            "stale": _flat.get("health:degraded", _DEFAULTS["stale"]),
            "critical": _flat.get("health:unhealthy", _DEFAULTS["critical"]),
            "missing": _flat.get("health:unknown", _DEFAULTS["missing"]),
        }
    except Exception:
        return _DEFAULTS

STATUS_EMOJI = _load_status_emoji()


def format_human(results: list[dict]) -> str:
    """Format results as a human-readable table."""
    lines = ["Pipeline Heartbeat Status", "=" * 60]

    for r in results:
        emoji = STATUS_EMOJI.get(r["status"], "?")
        age_str = f"{r['age_hours']}h" if r["age_hours"] is not None else "n/a"
        threshold_str = f"{r['threshold_hours']}h"
        lines.append(
            f"  {emoji} {r['name']:12s}  {r['status']:8s}  "
            f"age: {age_str:>8s}  threshold: {threshold_str:>5s}  "
            f"{r['description']}"
        )

    # Summary
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary_parts = []
    for status in ("critical", "stale", "fresh", "missing"):
        if counts.get(status, 0) > 0:
            summary_parts.append(f"{counts[status]} {status}")
    lines.append("-" * 60)
    lines.append(f"  Summary: {', '.join(summary_parts)}")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Check pipeline heartbeats for staleness")
    parser.add_argument("--json", action="store_true", help="Output JSON array")
    parser.add_argument("--quiet", action="store_true", help="No output, exit code only")
    parser.add_argument(
        "--config", type=Path, default=CONFIG_PATH, help=f"Path to heartbeats.yml (default: {CONFIG_PATH})"
    )
    args = parser.parse_args()

    config = load_config(args.config)
    results = check_all(config)

    # Determine exit code
    statuses = {r["status"] for r in results}
    if "critical" in statuses:
        exit_code = 2
    elif "stale" in statuses:
        exit_code = 1
    else:
        exit_code = 0

    # Auto-scratchpad on critical
    critical = [r for r in results if r["status"] == "critical"]
    if critical:
        scratchpad_dir = Path(config.get("scratchpad_dir", "~/.claude/local/scratchpad")).expanduser()
        alert_path = write_scratchpad_alert(critical, scratchpad_dir)
        if alert_path and not args.quiet:
            print(f"Scratchpad alert written: {alert_path}", file=sys.stderr)

    # Output
    if not args.quiet:
        if args.json:
            print(json.dumps(results, indent=2, default=str))
        else:
            print(format_human(results))

    sys.exit(exit_code)


if __name__ == "__main__":
    main()

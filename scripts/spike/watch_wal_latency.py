#!/usr/bin/env python3
"""task-534 Phase 0 spike — measure watchfiles → SQLite WAL latency.

Goal: prove p95 commit-to-fs-event latency before committing the daemon design.

Method:
  1. Create a fresh SQLite DB in WAL mode in a temp dir.
  2. Start `watchfiles.awatch(temp_dir)` in an asyncio task.
  3. Sequentially INSERT N rows. Record `t_commit_ns` right after `conn.commit()`.
  4. Watcher records `t_detect_ns` for each fs event keyed by the event count.
  5. Wait for all detections to arrive (with a hard timeout), then compute
     median / p95 / p99 latency.

The watcher does not have direct access to the row id at fs-event time — it
gets a directory-change event. We pair them by **arrival order**: each commit
advances a counter; the next fs event for the DB or its -wal companion is
assumed to correspond to that commit. This is approximately true for
sequential single-threaded writes with the writer holding the only conn.

Decision gate: if p95 > 250ms, escalate (kernel inotify backpressure,
overlayfs, btrfs+snap-pac interaction, etc.).

Usage:
    uv run python scripts/spike/watch_wal_latency.py
    uv run python scripts/spike/watch_wal_latency.py --events 200 --gap-ms 50
    uv run python scripts/spike/watch_wal_latency.py --dir /tmp/spike-wal
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
import statistics
import sys
import time
from pathlib import Path

DEFAULT_DIR = Path("/tmp/claude-logging-spike-wal")
DEFAULT_EVENTS = 100
DEFAULT_GAP_MS = 100
DETECTION_TIMEOUT_SEC = 10.0


def _setup_db(db_path: Path) -> None:
    """Create fresh WAL-mode SQLite DB with one table."""
    if db_path.exists():
        db_path.unlink()
    # Remove stale wal/shm files too
    for ext in ("-wal", "-shm"):
        sib = db_path.with_name(db_path.name + ext)
        if sib.exists():
            sib.unlink()

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            payload TEXT
        )
        """
    )
    con.commit()
    con.close()


async def _watcher(
    watch_dir: Path,
    detection_queue: asyncio.Queue,
    stop_event: asyncio.Event,
) -> None:
    """awatch the directory; push (t_detect_ns, path, change_type) for each event."""
    try:
        from watchfiles import awatch
    except ImportError:
        print("ERROR: watchfiles not installed in this venv", file=sys.stderr)
        await detection_queue.put(None)
        return

    try:
        async for changes in awatch(
            watch_dir,
            stop_event=stop_event,
            recursive=False,
            yield_on_timeout=False,
        ):
            t_detect_ns = time.monotonic_ns()
            for change_type, path in changes:
                await detection_queue.put((t_detect_ns, str(path), change_type.name))
    except Exception as exc:  # pragma: no cover - diagnostic
        print(f"watcher error: {exc!r}", file=sys.stderr)


async def _writer(
    db_path: Path,
    n_events: int,
    gap_ms: int,
    commits: list[tuple[int, int]],
) -> None:
    """Insert n_events rows, recording (event_idx, t_commit_ns) for each."""
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA synchronous=NORMAL")
    try:
        # warmup commit (don't count) — first commit can include extra fs events
        con.execute("INSERT INTO events (ts, payload) VALUES (?, ?)",
                    (time.strftime("%Y-%m-%dT%H:%M:%S"), "warmup"))
        con.commit()
        await asyncio.sleep(0.2)  # let warmup fs events drain

        for i in range(n_events):
            con.execute(
                "INSERT INTO events (ts, payload) VALUES (?, ?)",
                (time.strftime("%Y-%m-%dT%H:%M:%S"), f"event-{i}"),
            )
            con.commit()
            commits.append((i, time.monotonic_ns()))
            await asyncio.sleep(gap_ms / 1000.0)
    finally:
        con.close()


async def _drain_detections(
    detection_queue: asyncio.Queue,
    db_path: Path,
    n_expected: int,
    timeout_sec: float,
) -> list[tuple[int, str, str]]:
    """Collect fs detections until n_expected DB-file events or timeout."""
    db_name = db_path.name
    db_wal = db_name + "-wal"
    detections: list[tuple[int, str, str]] = []
    deadline = time.monotonic() + timeout_sec

    while len(detections) < n_expected:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            item = await asyncio.wait_for(detection_queue.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if item is None:
            break
        t_detect_ns, path, change_type = item
        if path.endswith(db_name) or path.endswith(db_wal):
            detections.append((t_detect_ns, path, change_type))
    return detections


def _pair_commits_to_detections(
    commits: list[tuple[int, int]],
    detections: list[tuple[int, str, str]],
) -> list[tuple[int, int]]:
    """For each commit, find the NEXT detection at-or-after it (wake-up latency).

    Detections are NOT consumed — multiple commits within one batch window share
    the same detection. This matches the real daemon's behavior: each fs event
    triggers an incremental SELECT that pulls all rows newer than last_seen_ts.

    Returns list of (event_idx, latency_ns). Commits with no subsequent
    detection are dropped (tail loss; the daemon's 5-min reconcile catches
    these).
    """
    paired: list[tuple[int, int]] = []
    detections_sorted = sorted(detections, key=lambda d: d[0])
    if not detections_sorted:
        return paired

    d_idx = 0
    for event_idx, t_commit in commits:
        # Advance d_idx to the first detection at or after t_commit
        while d_idx < len(detections_sorted) and detections_sorted[d_idx][0] < t_commit:
            d_idx += 1
        if d_idx >= len(detections_sorted):
            break  # tail loss
        t_detect = detections_sorted[d_idx][0]
        paired.append((event_idx, t_detect - t_commit))
        # do NOT advance d_idx — next commit may share this same detection

    return paired


def _summarize(latencies_ms: list[float]) -> dict:
    if not latencies_ms:
        return {"n": 0}
    sorted_ms = sorted(latencies_ms)
    n = len(sorted_ms)
    return {
        "n": n,
        "min_ms": round(sorted_ms[0], 3),
        "median_ms": round(statistics.median(sorted_ms), 3),
        "mean_ms": round(statistics.fmean(sorted_ms), 3),
        "p90_ms": round(sorted_ms[int(0.90 * (n - 1))], 3),
        "p95_ms": round(sorted_ms[int(0.95 * (n - 1))], 3),
        "p99_ms": round(sorted_ms[int(0.99 * (n - 1))], 3),
        "max_ms": round(sorted_ms[-1], 3),
        "stdev_ms": round(statistics.pstdev(sorted_ms), 3),
    }


async def run_spike(watch_dir: Path, db_path: Path, n_events: int, gap_ms: int) -> dict:
    detection_queue: asyncio.Queue = asyncio.Queue()
    stop_event = asyncio.Event()
    commits: list[tuple[int, int]] = []

    watcher_task = asyncio.create_task(_watcher(watch_dir, detection_queue, stop_event))
    # Give the watcher a moment to register inotify
    await asyncio.sleep(0.3)

    await _writer(db_path, n_events, gap_ms, commits)

    # Drain detections — wait up to timeout for fs events to arrive
    # Expect at least n_events detections (often 2x because of -wal + main file)
    detections = await _drain_detections(
        detection_queue,
        db_path,
        n_expected=n_events * 2,  # be generous
        timeout_sec=DETECTION_TIMEOUT_SEC,
    )

    stop_event.set()
    try:
        await asyncio.wait_for(watcher_task, timeout=2.0)
    except asyncio.TimeoutError:
        watcher_task.cancel()

    # Split detections by file type for diagnostics
    db_only = [d for d in detections if d[1].endswith(db_path.name)]
    wal_only = [d for d in detections if d[1].endswith(db_path.name + "-wal")]

    # Primary pairing: against the file that fires first/most often for WAL.
    # For WAL mode this is almost always the -wal file.
    primary = wal_only if len(wal_only) >= len(db_only) else db_only
    paired_primary = _pair_commits_to_detections(commits, primary)
    latencies_primary_ms = [ns / 1e6 for _, ns in paired_primary]

    # Also pair against ANY db-related detection (first arrival wins)
    paired_any = _pair_commits_to_detections(commits, detections)
    latencies_any_ms = [ns / 1e6 for _, ns in paired_any]

    return {
        "config": {
            "watch_dir": str(watch_dir),
            "db_path": str(db_path),
            "n_events": n_events,
            "gap_ms": gap_ms,
        },
        "counts": {
            "commits": len(commits),
            "fs_events_total": len(detections),
            "fs_events_db_file": len(db_only),
            "fs_events_wal_file": len(wal_only),
            "paired_primary": len(paired_primary),
            "paired_any": len(paired_any),
        },
        "primary_target": "db-wal" if primary is wal_only else "db",
        "latency_primary_ms": _summarize(latencies_primary_ms),
        "latency_any_ms": _summarize(latencies_any_ms),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--events", type=int, default=DEFAULT_EVENTS)
    ap.add_argument("--gap-ms", type=int, default=DEFAULT_GAP_MS)
    ap.add_argument("--json", action="store_true", help="print machine-readable JSON only")
    ap.add_argument(
        "--keep-dir",
        action="store_true",
        help="do not wipe --dir; assume it already contains spike.db (for btrfs/real-fs probes)",
    )
    args = ap.parse_args()

    watch_dir: Path = args.dir
    if not args.keep_dir:
        if watch_dir.exists():
            shutil.rmtree(watch_dir)
        watch_dir.mkdir(parents=True, exist_ok=True)
    db_path = watch_dir / "spike.db"
    if not args.keep_dir or not db_path.exists():
        _setup_db(db_path)

    if not args.json:
        print(f"spike dir : {watch_dir}")
        print(f"db_path   : {db_path}")
        print(f"events    : {args.events} @ {args.gap_ms}ms gap")
        print(f"running   ...")

    result = asyncio.run(run_spike(watch_dir, db_path, args.events, args.gap_ms))

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print()
        print(json.dumps(result, indent=2))
        prim = result["latency_primary_ms"]
        if prim.get("n", 0) >= max(10, args.events // 2):
            gate_pass = prim["p95_ms"] <= 250.0
            print()
            print(f"DECISION GATE — p95 = {prim['p95_ms']} ms "
                  f"(threshold 250 ms): {'PASS' if gate_pass else 'FAIL'}")
        else:
            print()
            print("DECISION GATE — INSUFFICIENT DATA "
                  f"(only {prim.get('n', 0)} paired observations)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

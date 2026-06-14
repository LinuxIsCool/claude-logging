#!/usr/bin/env python3
"""task-534 Phase 1 — long-running watchfiles-driven rollup daemon.

Replaces the 60s `claude-logging-rollup.timer` for the hot path: each
per-project SQLite WAL commit triggers an inotify fs event on
`<slug>/db/logging.db-wal`, the daemon wakes within ~50-100ms, runs the
incremental `rollup_project()` for that shard only, and commits the
delta to `_index/index.db`.

Design summary (see task-534 vision + Phase 0 spike SYNTHESIS.md):

  * Hot path: watchfiles.awatch(LOGGING_ROOT, recursive=True) →
    per-shard 100ms debounce → rollup_project() → idx commit.
  * Safety net: every 5 minutes a full reconcile pass over every shard
    in rollup_state, identical to scripts/v2/rollup_index.py. Catches
    anything inotify missed (rename, snapper rollback, etc).
  * Schema drift: per-shard try/except. A shard that errors (e.g.
    OperationalError: no such column: persona) is marked degraded,
    logged once, skipped on subsequent ticks until restart or until
    the shard's writer DB recovers.
  * Health: every 10 seconds the daemon writes
    `_index/daemon-health.json` with uptime, fs-event count, insert
    count, per-shard max_ts, lag, and the degraded-shard list. The
    webui SSE phase will read this same surface; for now it's the
    ground-truth health probe.

Lifecycle:

    uv run --extra streaming python scripts/v2/rollup_daemon.py
    # Or via systemd:
    systemctl --user start claude-logging-rollup-daemon

Idempotent against the existing 60s `claude-logging-rollup.timer` —
both can run in parallel during the 14-day soak. INSERT OR REPLACE on
event_id means any double-pull is a no-op.

Exit: SIGTERM / SIGINT triggers graceful shutdown — final reconcile
pass, then drain. Restart is safe at any point; the 5-min reconcile
catches up anything missed in the gap.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from contextlib import suppress
from pathlib import Path

# Reuse the existing rollup logic verbatim — single source of truth.
_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))
from rollup_index import (  # noqa: E402
    HOSTNAME,
    INDEX_DB,
    LOGGING_ROOT,
    discover_dbs,
    reconcile_project,
    rollup_project,
    update_hostname_state,
    update_rollup_state,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEBOUNCE_MS = 100               # collapse fs events per shard within this window
RECONCILE_INTERVAL_SEC = 300    # full safety-net pass every 5 minutes
HEALTH_INTERVAL_SEC = 10        # rewrite health JSON every 10s
HEALTH_PATH = LOGGING_ROOT / "_index" / "daemon-health.json"

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


def _shard_event_count(db_path: Path) -> int:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    try:
        return con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        con.close()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FORMAT,
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Path → slug mapping
# ---------------------------------------------------------------------------

def _slug_for_path(path: str) -> str | None:
    """Extract the project slug from a watched DB file path.

    Expected layout:
        ~/.claude/local/logging/<slug>/db/{logging.db,logging.db-wal,...}
    """
    try:
        p = Path(path).resolve()
        rel = p.relative_to(LOGGING_ROOT.resolve())
    except (ValueError, OSError):
        return None
    parts = rel.parts
    # parts[0] is the slug; parts[1] should be "db"; parts[2] starts with logging.db
    if len(parts) < 3 or parts[1] != "db":
        return None
    if not parts[2].startswith("logging.db"):
        return None
    return parts[0]


def _path_filter(_change, path: str) -> bool:
    """watchfiles filter — only WAL/DB files in valid shard locations.

    `_change` is the watchfiles.Change enum value; not used (we treat
    add/modify/delete identically — they all signal "this DB moved, re-check
    for new rows").
    """
    if not (path.endswith("logging.db") or path.endswith("logging.db-wal")):
        return False
    return _slug_for_path(path) is not None


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class RollupDaemon:
    def __init__(self, idx_db: Path = INDEX_DB) -> None:
        self.idx_db = idx_db
        self.idx_con: sqlite3.Connection | None = None
        self.stop_event = asyncio.Event()

        # Pending shard slugs awaiting flush (after debounce window expires)
        self._pending: dict[str, float] = {}   # slug -> first-seen monotonic ts
        self._pending_lock = asyncio.Lock()

        # Per-shard state for health surface
        self.degraded: dict[str, str] = {}     # slug -> last error string
        self.shard_max_ts: dict[str, str] = {} # slug -> last seen max_ts
        self.shard_last_insert: dict[str, float] = {}  # slug -> wall ts of last insert

        # Counters
        self.t_start = time.time()
        self.fs_events_total = 0
        self.fs_events_eligible = 0
        self.flushes_total = 0
        self.events_inserted_total = 0
        self.reconciles_total = 0
        self.last_reconcile_ts = 0.0
        self.last_fs_event_ts = 0.0
        self.last_insert_ts = 0.0
        self.completeness_index_total = 0
        self.completeness_source_total = 0

    # ----- index DB connection -----

    def _open_index(self) -> sqlite3.Connection:
        if self.idx_con is None:
            # check_same_thread=False: all rollup work runs through
            # asyncio.to_thread(), which serializes one-at-a-time on a worker
            # thread that is NOT the connection's creation thread. The daemon
            # never issues concurrent queries against this connection, so the
            # thread-affinity guard is unnecessary friction.
            con = sqlite3.connect(
                self.idx_db, timeout=30.0, check_same_thread=False
            )
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            self.idx_con = con
        return self.idx_con

    def _close_index(self) -> None:
        if self.idx_con is not None:
            with suppress(Exception):
                self.idx_con.commit()
                self.idx_con.close()
            self.idx_con = None

    # ----- shard rollup wrapper -----

    def _rollup_one(self, slug: str, db_path: Path) -> tuple[int, str | None]:
        """Run rollup_project for one shard, with schema-drift guard."""
        con = self._open_index()
        try:
            inserted, max_ts = rollup_project(con, slug, db_path)
            update_rollup_state(con, slug, max_ts, inserted)
            con.commit()
            if slug in self.degraded:
                logging.info("shard recovered: %s", slug)
                self.degraded.pop(slug, None)
            return inserted, max_ts
        except sqlite3.OperationalError as e:
            msg = str(e)
            if self.degraded.get(slug) != msg:
                logging.warning("shard degraded: %s -> %s", slug, msg)
                self.degraded[slug] = msg
            return 0, None
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if self.degraded.get(slug) != msg:
                logging.exception("shard error: %s -> %s", slug, msg)
                self.degraded[slug] = msg
            return 0, None

    def _reconcile_one(self, slug: str, db_path: Path) -> tuple[int, int]:
        """True completeness reconcile for one shard, with degraded-shard guard.
        Returns (inserted, index_count)."""
        con = self._open_index()
        try:
            inserted, idx_count = reconcile_project(con, slug, db_path)
            con.commit()
            if slug in self.degraded:
                logging.info("shard recovered: %s", slug)
                self.degraded.pop(slug, None)
            return inserted, idx_count
        except sqlite3.OperationalError as e:
            msg = str(e)
            if self.degraded.get(slug) != msg:
                logging.warning("shard degraded (reconcile): %s -> %s", slug, msg)
                self.degraded[slug] = msg
            return 0, 0
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if self.degraded.get(slug) != msg:
                logging.exception("shard error (reconcile): %s -> %s", slug, msg)
                self.degraded[slug] = msg
            return 0, 0

    # ----- watcher loop -----

    async def watch_loop(self) -> None:
        from watchfiles import awatch

        logging.info("watch_loop: starting awatch on %s", LOGGING_ROOT)
        try:
            async for changes in awatch(
                LOGGING_ROOT,
                stop_event=self.stop_event,
                watch_filter=_path_filter,
                recursive=True,
                yield_on_timeout=False,
            ):
                self.last_fs_event_ts = time.time()
                hit_slugs: set[str] = set()
                for _change_type, path in changes:
                    self.fs_events_total += 1
                    slug = _slug_for_path(path)
                    if slug:
                        self.fs_events_eligible += 1
                        hit_slugs.add(slug)
                if hit_slugs:
                    async with self._pending_lock:
                        now = time.monotonic()
                        for s in hit_slugs:
                            self._pending.setdefault(s, now)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("watch_loop crashed")
            self.stop_event.set()

    async def debounce_flush_loop(self) -> None:
        """Every DEBOUNCE_MS, flush shards whose first-pending time is old enough."""
        gap_sec = DEBOUNCE_MS / 1000.0
        while not self.stop_event.is_set():
            await asyncio.sleep(gap_sec)
            now = time.monotonic()
            ready: list[str] = []
            async with self._pending_lock:
                for slug, first_seen in list(self._pending.items()):
                    if now - first_seen >= gap_sec:
                        ready.append(slug)
                        self._pending.pop(slug, None)
            if not ready:
                continue
            for slug in ready:
                db_path = LOGGING_ROOT / slug / "db" / "logging.db"
                if not db_path.exists():
                    continue
                t0 = time.monotonic()
                inserted, max_ts = await asyncio.to_thread(
                    self._rollup_one, slug, db_path
                )
                dur_ms = (time.monotonic() - t0) * 1000
                self.flushes_total += 1
                if inserted > 0:
                    self.events_inserted_total += inserted
                    self.last_insert_ts = time.time()
                    self.shard_last_insert[slug] = self.last_insert_ts
                    if max_ts:
                        self.shard_max_ts[slug] = max_ts
                    logging.info(
                        "flush %s: +%d (%.1fms)", slug, inserted, dur_ms
                    )
                else:
                    logging.debug("flush %s: 0 (%.1fms)", slug, dur_ms)

    # ----- reconcile loop (safety net) -----

    async def reconcile_loop(self) -> None:
        """Every RECONCILE_INTERVAL_SEC, full pass identical to rollup_index.py."""
        # First reconcile happens immediately at startup to catch any drift since last run.
        await asyncio.sleep(2)  # give the watcher a head-start
        while not self.stop_event.is_set():
            await self._run_reconcile()
            for _ in range(RECONCILE_INTERVAL_SEC):
                if self.stop_event.is_set():
                    return
                await asyncio.sleep(1)

    async def _run_reconcile(self) -> None:
        t0 = time.time()
        dbs = await asyncio.to_thread(discover_dbs)
        total_inserted = 0
        index_total = 0
        source_total = 0
        for slug, db in dbs:
            inserted, idx_count = await asyncio.to_thread(self._reconcile_one, slug, db)
            total_inserted += inserted
            index_total += idx_count
            try:
                # Second source read (reconcile_project already counted internally);
                # the small TOCTOU vs in-flight writes self-heals on the next pass.
                src_count = await asyncio.to_thread(_shard_event_count, db)
            except Exception:
                src_count = idx_count
            source_total += src_count
            if inserted > 0:
                self.shard_last_insert[slug] = time.time()
        self.completeness_index_total = index_total
        self.completeness_source_total = source_total
        if self.idx_con is not None:
            await asyncio.to_thread(
                update_hostname_state, self.idx_con, HOSTNAME, len(dbs), None
            )
            await asyncio.to_thread(self.idx_con.commit)
        self.reconciles_total += 1
        self.last_reconcile_ts = time.time()
        if total_inserted > 0:
            self.events_inserted_total += total_inserted
            self.last_insert_ts = time.time()
        dur = time.time() - t0
        logging.info(
            "reconcile #%d: %d shards, +%d recovered, %d/%d indexed, %.2fs",
            self.reconciles_total, len(dbs), total_inserted,
            index_total, source_total, dur,
        )

    # ----- health loop -----

    async def health_loop(self) -> None:
        HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        while not self.stop_event.is_set():
            try:
                await asyncio.to_thread(self._write_health)
            except Exception:
                logging.exception("health write failed")
            for _ in range(HEALTH_INTERVAL_SEC):
                if self.stop_event.is_set():
                    return
                await asyncio.sleep(1)

    def _write_health(self) -> None:
        now = time.time()
        # Lag = wall-clock seconds since the most recent ts we've inserted.
        # Approximate: use the max over self.shard_max_ts (ISO strings sort fine).
        max_ts_iso = max(self.shard_max_ts.values()) if self.shard_max_ts else None
        payload = {
            "pid": os.getpid(),
            "hostname": HOSTNAME,
            "started_at": self.t_start,
            "uptime_sec": int(now - self.t_start),
            "now": now,
            "counters": {
                "fs_events_total": self.fs_events_total,
                "fs_events_eligible": self.fs_events_eligible,
                "flushes_total": self.flushes_total,
                "events_inserted_total": self.events_inserted_total,
                "reconciles_total": self.reconciles_total,
            },
            "last_fs_event_ts": self.last_fs_event_ts,
            "last_insert_ts": self.last_insert_ts,
            "last_reconcile_ts": self.last_reconcile_ts,
            "max_event_ts_seen": max_ts_iso,
            "degraded_shards": dict(self.degraded),
            "degraded_count": len(self.degraded),
            "tracked_shards": len(self.shard_max_ts),
            "index_completeness": {
                "index_total": self.completeness_index_total,
                "source_total": self.completeness_source_total,
                "missing": max(0, self.completeness_source_total - self.completeness_index_total),
                "pct": round(
                    100.0 * self.completeness_index_total / self.completeness_source_total, 3
                ) if self.completeness_source_total else 100.0,
            },
            "pending_shards": len(self._pending),
        }
        tmp = HEALTH_PATH.with_suffix(HEALTH_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(HEALTH_PATH)

    # ----- main run -----

    async def run(self) -> None:
        # SIGTERM / SIGINT → graceful shutdown
        loop = asyncio.get_running_loop()

        def _request_stop(signame: str) -> None:
            logging.info("received %s; stopping", signame)
            self.stop_event.set()

        for signame in ("SIGTERM", "SIGINT"):
            with suppress(NotImplementedError):
                loop.add_signal_handler(
                    getattr(signal, signame),
                    _request_stop,
                    signame,
                )

        # Open index DB up front so the first event is fast
        self._open_index()

        tasks = [
            asyncio.create_task(self.watch_loop(), name="watch"),
            asyncio.create_task(self.debounce_flush_loop(), name="flush"),
            asyncio.create_task(self.reconcile_loop(), name="reconcile"),
            asyncio.create_task(self.health_loop(), name="health"),
        ]

        await self.stop_event.wait()
        logging.info("shutdown: cancelling background tasks")
        for t in tasks:
            t.cancel()
        for t in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await t

        # Final flush + reconcile so no event is lost in the shutdown window
        logging.info("shutdown: final reconcile pass")
        with suppress(Exception):
            await self._run_reconcile()
        self._close_index()
        logging.info(
            "shutdown complete — uptime %ds, %d events inserted, %d reconciles, %d degraded shards",
            int(time.time() - self.t_start),
            self.events_inserted_total,
            self.reconciles_total,
            len(self.degraded),
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    _setup_logging(args.verbose)

    if not INDEX_DB.exists():
        logging.error("index DB missing at %s — run init_cross_project_index_db.py", INDEX_DB)
        return 1

    daemon = RollupDaemon()
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

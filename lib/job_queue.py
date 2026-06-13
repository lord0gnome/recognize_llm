"""Persistent SQLite-backed job queue with a small worker pool.

This is the bulk path: upload events and backfill both enqueue ``(user_id, file_id)`` jobs which a
configurable number of worker threads drain at a controlled rate, so the local vision model is
never flooded. State lives on disk so the queue survives an exApp restart (resumable backfill).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import processor
import settings as settings_mod
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import LogLvl, persistent_storage

MAX_ATTEMPTS = 3
_DB = os.path.join(persistent_storage(), "recognize_llm_queue.db")
_claim_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_DB, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with _connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                user_id   TEXT NOT NULL,
                file_id   INTEGER NOT NULL,
                status    TEXT NOT NULL DEFAULT 'pending',
                source    TEXT NOT NULL DEFAULT 'manual',
                force     INTEGER NOT NULL DEFAULT 0,
                attempts  INTEGER NOT NULL DEFAULT 0,
                error     TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, file_id)
            )
            """
        )


def enqueue(user_id: str, file_id: int, source: str = "manual", force: bool = False) -> None:
    # Without force (backfill/event), don't reset already-done or in-flight jobs.
    # With force (manual re-describe), always reset so the file is reprocessed.
    conflict = (
        "DO UPDATE SET status='pending', source=excluded.source, force=excluded.force, "
        "attempts=0, error='', updated_at=excluded.updated_at"
        + ("" if force else " WHERE jobs.status NOT IN ('done', 'processing')")
    )
    with _connect() as con:
        con.execute(
            f"""
            INSERT INTO jobs (user_id, file_id, status, source, force, attempts, error, updated_at)
            VALUES (?, ?, 'pending', ?, ?, 0, '', ?)
            ON CONFLICT(user_id, file_id) {conflict}
            """,
            (user_id, int(file_id), source, 1 if force else 0, int(time.time())),
        )


def status() -> dict:
    with _connect() as con:
        rows = con.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status").fetchall()
    counts = {r["status"]: r["c"] for r in rows}
    return {
        "pending": counts.get("pending", 0),
        "processing": counts.get("processing", 0),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "total": sum(counts.values()),
    }


def _claim() -> sqlite3.Row | None:
    """Atomically move one pending job to 'processing' and return it."""
    with _claim_lock, _connect() as con:
        row = con.execute(
            "SELECT user_id, file_id, source, force, attempts FROM jobs WHERE status='pending' "
            "ORDER BY updated_at LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        con.execute(
            "UPDATE jobs SET status='processing', updated_at=? WHERE user_id=? AND file_id=?",
            (int(time.time()), row["user_id"], row["file_id"]),
        )
        return row


def _finish(row: sqlite3.Row, status_: str, error: str = "") -> None:
    with _connect() as con:
        con.execute(
            "UPDATE jobs SET status=?, attempts=attempts+1, error=?, updated_at=? "
            "WHERE user_id=? AND file_id=?",
            (status_, error[:1000], int(time.time()), row["user_id"], row["file_id"]),
        )


class Workers:
    """Owns the worker threads. ``start()`` is idempotent w.r.t. the configured concurrency."""

    def __init__(self):
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self, concurrency: int) -> None:
        self._stop.clear()
        while len(self._threads) < concurrency:
            t = threading.Thread(target=self._loop, name=f"recognize-llm-worker-{len(self._threads)}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        nc = NextcloudApp()
        idle = 0.0
        while not self._stop.is_set():
            row = _claim()
            if row is None:
                idle = min(idle + 0.5, 5.0)
                time.sleep(idle)
                continue
            idle = 0.0
            try:
                cfg = settings_mod.load(nc)
                res = processor.process_file(
                    nc, row["user_id"], int(row["file_id"]), cfg, force=bool(row["force"])
                )
                _finish(row, "done" if res.status in ("done", "skipped") else "failed", res.reason)
            except Exception as e:
                retry = row["attempts"] + 1 < MAX_ATTEMPTS
                _finish(row, "pending" if retry else "failed", str(e))
                nc.log(LogLvl.ERROR, f"recognize_llm: job user={row['user_id']} file={row['file_id']} error: {e}")
                if retry:
                    time.sleep(2.0)

"""Persistent SQLite-backed job queue with a small worker pool.

This is the bulk path: upload events and backfill both enqueue ``(user_id, file_id)`` jobs which a
configurable number of worker threads drain at a controlled rate, so the local vision model is
never flooded. State lives on disk so the queue survives an exApp restart (resumable backfill).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

import settings as settings_mod
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import LogLvl, persistent_storage

MAX_ATTEMPTS = 3
# A job still 'processing' after this long is treated as wedged (dead/blocked worker) and requeued.
# Must exceed the worst-case single attempt: ffprobe(30s) + 9×ffmpeg(30s) + vision(≤180s) ≈ 8 min.
STUCK_JOB_TIMEOUT = 900
# The worker reuses one NextcloudApp for its lifetime; if its session goes stale (dead keep-alive
# connection / stale capabilities) EVERY call starts failing (400) or hanging and no job completes.
# After this many consecutive failures, rebuild the session — a fresh nc is exactly what a container
# restart used to provide. Cooldown prevents thrashing when Nextcloud itself is genuinely down.
NC_REFRESH_AFTER_FAILURES = 3
NC_REFRESH_COOLDOWN = 30
_DB = os.path.join(persistent_storage(), "recognize_llm_queue.db")
_claim_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    # 60s busy timeout so a brief lock from a concurrent writer (clustering / thumbnail passes) waits
    # instead of raising OperationalError; WAL + synchronous=NORMAL keep writers short and concurrent.
    con = sqlite3.connect(_DB, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA synchronous=NORMAL")
    con.row_factory = sqlite3.Row
    return con


def _safe_log(nc, msg: str) -> None:
    """Log to Nextcloud without ever raising — logging failures must not bubble into the worker loop."""
    try:
        nc.log(LogLvl.ERROR, msg)
    except Exception:
        pass


def _add_column(con: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Idempotently add a column (SQLite has no ADD COLUMN IF NOT EXISTS)."""
    cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


_RECENT_LIMIT = 50


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
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS recent_results (
                file_id      INTEGER NOT NULL,
                user_id      TEXT NOT NULL,
                processed_at INTEGER NOT NULL,
                name         TEXT NOT NULL DEFAULT '',
                path         TEXT NOT NULL DEFAULT '',
                description  TEXT NOT NULL DEFAULT '',
                tags         TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (file_id)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS face_embeddings (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL,
                file_id    INTEGER NOT NULL,
                face_index INTEGER NOT NULL,
                embedding  BLOB NOT NULL,
                cluster_id INTEGER NOT NULL DEFAULT -1,
                created_at INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user_id, file_id, face_index)
            )
            """
        )
        # M7: face review needs a bounding box (to crop thumbnails) and a detection score
        # (to pick the sharpest face as a person's representative). Added post-hoc so existing
        # embedding rows survive; new columns default empty and get populated on next extraction.
        _add_column(con, "face_embeddings", "bbox", "TEXT NOT NULL DEFAULT ''")
        _add_column(con, "face_embeddings", "det_score", "REAL NOT NULL DEFAULT 0")
        con.execute("CREATE INDEX IF NOT EXISTS idx_face_emb_person ON face_embeddings(user_id, cluster_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_face_emb_file ON face_embeddings(user_id, file_id)")

        # Small JPEG face crops (~112px) shown in the People review UI. Kept in a side table so
        # the embeddings table stays lean and thumbnails can be pruned independently.
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS face_thumbs (
                face_id INTEGER PRIMARY KEY,
                jpeg    BLOB NOT NULL
            )
            """
        )
        # A "person" is a stable identity: its person_id is preserved across re-clustering runs by
        # matching cluster centroids (not DBSCAN's volatile labels), so user-given names, merges and
        # splits survive. cluster_id in face_embeddings holds this person_id (-1 = unassigned).
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS face_persons (
                user_id        TEXT NOT NULL,
                person_id      INTEGER NOT NULL,
                name           TEXT NOT NULL DEFAULT '',
                tag_id         INTEGER NOT NULL DEFAULT -1,
                centroid       BLOB,
                sample_face_id INTEGER NOT NULL DEFAULT -1,
                face_count     INTEGER NOT NULL DEFAULT 0,
                ignored        INTEGER NOT NULL DEFAULT 0,
                updated_at     INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, person_id)
            )
            """
        )
        # Monotonic per-user person_id allocator so ids are never reused (stable across deletes).
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS face_meta (
                user_id        TEXT PRIMARY KEY,
                next_person_id INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        # Legacy table from the pre-M7 clustering prototype; superseded by face_persons. Kept
        # (unused) so an in-place upgrade doesn't drop data mid-migration.
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS face_clusters (
                user_id       TEXT NOT NULL,
                cluster_id    INTEGER NOT NULL,
                tag_name      TEXT NOT NULL,
                file_ids_json TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (user_id, cluster_id)
            )
            """
        )
        # Any job left in 'processing' on startup is orphaned (container died mid-job).
        # Reset them so workers pick them up again.
        con.execute(
            "UPDATE jobs SET status='pending', updated_at=? WHERE status='processing'",
            (int(time.time()),),
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


def status(user_id: str | None = None) -> dict:
    with _connect() as con:
        if user_id:
            rows = con.execute(
                "SELECT status, COUNT(*) c FROM jobs WHERE user_id=? GROUP BY status", (user_id,)
            ).fetchall()
        else:
            rows = con.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status").fetchall()
    counts = {r["status"]: r["c"] for r in rows}
    return {
        "pending": counts.get("pending", 0),
        "processing": counts.get("processing", 0),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "total": sum(counts.values()),
    }


def record_recent(user_id: str, file_id: int, name: str, path: str, description: str, tags: list[str]) -> None:
    with _connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO recent_results "
            "(file_id, user_id, processed_at, name, path, description, tags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (file_id, user_id, int(time.time()), name, path, description, json.dumps(tags)),
        )
        con.execute(
            "DELETE FROM recent_results WHERE file_id NOT IN "
            "(SELECT file_id FROM recent_results ORDER BY processed_at DESC LIMIT ?)",
            (_RECENT_LIMIT,),
        )


def retry_failed(user_id: str | None = None) -> int:
    """Reset failed jobs to pending. Returns the number of jobs reset."""
    with _connect() as con:
        if user_id:
            cur = con.execute(
                "UPDATE jobs SET status='pending', attempts=0, error='', updated_at=? "
                "WHERE status='failed' AND user_id=?",
                (int(time.time()), user_id),
            )
        else:
            cur = con.execute(
                "UPDATE jobs SET status='pending', attempts=0, error='', updated_at=? WHERE status='failed'",
                (int(time.time()),),
            )
        return cur.rowcount


def get_recent(limit: int = 20, user_id: str | None = None) -> list[dict]:
    with _connect() as con:
        if user_id:
            rows = con.execute(
                "SELECT file_id, user_id, processed_at, name, path, description, tags "
                "FROM recent_results WHERE user_id=? ORDER BY processed_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT file_id, user_id, processed_at, name, path, description, tags "
                "FROM recent_results ORDER BY processed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [
        {
            "file_id": r[0],
            "user_id": r[1],
            "processed_at": r[2],
            "name": r[3],
            "path": r[4],
            "description": r[5],
            "tags": json.loads(r[6]),
        }
        for r in rows
    ]


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


def reap_stuck_jobs(max_age: int = STUCK_JOB_TIMEOUT) -> int:
    """Requeue jobs wedged in 'processing' (dead/blocked worker); fail those out of attempts.

    Runtime counterpart to the startup-only reset in ``init_db``: lets a stalled job self-heal without
    a container restart. Returns the number of rows reset.
    """
    cutoff = int(time.time()) - max_age
    with _connect() as con:
        cur = con.execute(
            "UPDATE jobs SET "
            "  status=CASE WHEN attempts + 1 < ? THEN 'pending' ELSE 'failed' END, "
            "  attempts=attempts + 1, "
            "  error='reaped: stuck in processing', "
            "  updated_at=? "
            "WHERE status='processing' AND updated_at < ?",
            (MAX_ATTEMPTS, int(time.time()), cutoff),
        )
        return cur.rowcount


class Workers:
    """Owns the worker threads plus a supervisor that respawns dead workers and reaps stuck jobs."""

    def __init__(self):
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._concurrency = 0
        self._supervisor: threading.Thread | None = None

    def start(self, concurrency: int) -> None:
        self._stop.clear()
        self._concurrency = max(self._concurrency, concurrency)
        self._ensure_workers()
        if self._supervisor is None or not self._supervisor.is_alive():
            self._supervisor = threading.Thread(target=self._supervise, name="recognize-llm-supervisor", daemon=True)
            self._supervisor.start()

    def _ensure_workers(self) -> None:
        """Drop dead threads and (re)spawn up to the target concurrency."""
        self._threads = [t for t in self._threads if t.is_alive()]
        while len(self._threads) < self._concurrency:
            t = threading.Thread(target=self._loop, name=f"recognize-llm-worker-{len(self._threads)}", daemon=True)
            t.start()
            self._threads.append(t)

    def _supervise(self) -> None:
        """Every 60s: respawn any worker that died, and requeue jobs wedged in 'processing'."""
        nc = NextcloudApp()
        while not self._stop.wait(60):
            try:
                before = len([t for t in self._threads if t.is_alive()])
                self._ensure_workers()
                if before < self._concurrency:
                    _safe_log(nc, f"recognize_llm: supervisor respawned worker(s) ({before}/{self._concurrency} alive)")
                reaped = reap_stuck_jobs()
                if reaped:
                    _safe_log(nc, f"recognize_llm: supervisor requeued {reaped} stuck job(s)")
            except Exception as e:
                _safe_log(nc, f"recognize_llm: supervisor error: {e}")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        nc = NextcloudApp()
        idle = 0.0
        fails = 0            # consecutive job failures — a run of these means the nc session went bad
        last_refresh = 0.0
        while not self._stop.is_set():
            # Outer guard: a failure in _claim / _finish / logging must NEVER kill the worker thread,
            # or the (single) worker dies silently and the queue stalls until a container restart.
            try:
                row = _claim()
                if row is None:
                    idle = min(idle + 0.5, 5.0)
                    time.sleep(idle)
                    continue
                idle = 0.0
                ok = self._process_one(nc, row)
                fails = 0 if ok else fails + 1
                # A streak of failures = poisoned session (every by_id/download 400s or hangs).
                # Rebuild the NextcloudApp to get fresh connections + capabilities, like a restart does.
                if fails >= NC_REFRESH_AFTER_FAILURES and (time.time() - last_refresh) > NC_REFRESH_COOLDOWN:
                    _safe_log(nc, f"recognize_llm: {fails} consecutive failures — refreshing Nextcloud session")
                    try:
                        nc = NextcloudApp()
                    except Exception as e:
                        _safe_log(nc, f"recognize_llm: session refresh failed: {e}")
                    last_refresh = time.time()
                    fails = 0
            except Exception as e:
                _safe_log(nc, f"recognize_llm: worker loop error (recovered): {e}")
                time.sleep(2.0)

    def _process_one(self, nc, row: sqlite3.Row) -> bool:
        """Process one job. Returns True if it completed (done/skipped), False on failure."""
        import processor
        try:
            cfg = settings_mod.load(nc)
            res = processor.process_file(
                nc, row["user_id"], int(row["file_id"]), cfg, force=bool(row["force"])
            )
            _finish(row, "done" if res.status in ("done", "skipped") else "failed", res.reason)
            if res.status == "done" and res.caption:
                record_recent(
                    row["user_id"], int(row["file_id"]),
                    res.name, res.path,
                    res.caption.description, res.caption.tags,
                )
            return res.status in ("done", "skipped")
        except Exception as e:
            retry = row["attempts"] + 1 < MAX_ATTEMPTS
            _finish(row, "pending" if retry else "failed", str(e))
            _safe_log(nc, f"recognize_llm: job user={row['user_id']} file={row['file_id']} error: {e}")
            if retry:
                time.sleep(2.0)
            return False

"""Live, in-process view of what the queue engine is doing — the "is it working or stalled?" answer.

The job table says what is queued; this says what is happening *now*: per worker slot, which file
is being handled and at which stage (downloading, captioning, faces…), whether a worker is paused
on a vision outage or backing off after an infrastructure error, plus a short ring of notable
events. ``engine_status`` folds that and the queue table into one verdict for the dashboard.

Process-local by design: it describes this container's threads, and resets on restart (the
dashboard then falls back to the job table for "last completed").
"""

from __future__ import annotations

import threading
import time
from collections import deque

_lock = threading.Lock()
_slots: dict[int, dict] = {}
_events: deque = deque(maxlen=40)
_local = threading.local()
STARTED_AT = time.time()
# Last request_timeout a worker loaded (kept here so status polls never hit Nextcloud for it).
request_timeout = 180

# Slot states
IDLE = "idle"            # polling an empty queue
PROCESSING = "processing"
WAITING_VISION = "waiting_vision"  # vision backend unreachable, probing until it answers
BACKOFF = "backoff"      # transient Nextcloud/network failure, sleeping before a retry

# A stage running longer than this is reported as stalled. Captioning gets the configured HTTP
# timeout on top (a slow model is not a stall until the request itself would have timed out).
STAGE_STALL_SECONDS = 300
NO_PROGRESS_SECONDS = 900


def bind_slot(slot: int) -> None:
    """Called once by each worker thread so stage() calls from deep inside know their slot."""
    _local.slot = slot


def set_slot(slot: int, state: str, job: dict | None = None, detail: str = "", until: float = 0.0) -> None:
    now = time.time()
    with _lock:
        _slots[slot] = {
            "slot": slot,
            "state": state,
            "since": now,
            "job": job,
            "stage": "",
            "stage_since": now,
            "detail": detail,
            "until": until,
        }


def clear_slot(slot: int) -> None:
    with _lock:
        _slots.pop(slot, None)


def stage(stage_name: str, /, **job_fields) -> None:
    """Record the current stage of the calling worker's job (no-op outside a worker thread)."""
    slot = getattr(_local, "slot", None)
    if slot is None:
        return
    with _lock:
        s = _slots.get(slot)
        if s is None or s["state"] != PROCESSING:
            return
        s["stage"] = stage_name
        s["stage_since"] = time.time()
        if job_fields and s.get("job") is not None:
            s["job"].update({k: v for k, v in job_fields.items() if v is not None})


def event(message: str, level: str = "info") -> None:
    with _lock:
        _events.appendleft({"ts": time.time(), "level": level, "message": message})


def snapshot() -> tuple[list[dict], list[dict]]:
    with _lock:
        slots = [dict(s, job=dict(s["job"]) if s.get("job") else None) for s in _slots.values()]
        events = list(_events)
    return sorted(slots, key=lambda s: s["slot"]), events


def _fmt_age(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


def engine_status(*, counts: dict, throughput: dict, workers_alive: int, concurrency: int,
                  include_details: bool, request_timeout: int | None = None,
                  now: float | None = None) -> dict:
    """One verdict + the evidence for it.

    health: working | idle | paused | degraded | stalled | stopped
    include_details=False drops file names/paths and event text (non-admin view).
    """
    now = now or time.time()
    if request_timeout is None:
        request_timeout = globals()["request_timeout"]
    slots, events = snapshot()
    pending = counts.get("pending", 0)
    last_done = throughput.get("last_done_ts") or 0

    def stall_limit(s: dict) -> float:
        extra = request_timeout if s.get("stage") == "captioning" else 0
        return STAGE_STALL_SECONDS + extra

    processing = [s for s in slots if s["state"] == PROCESSING]
    stuck = [s for s in processing if now - s["stage_since"] > stall_limit(s)]
    waiting = [s for s in slots if s["state"] == WAITING_VISION]
    backoff = [s for s in slots if s["state"] == BACKOFF]

    if concurrency > 0 and workers_alive == 0:
        health, message = "stopped", "No worker is running. Restart the app (or wait for the supervisor, ≤60 s)."
    elif waiting:
        s = waiting[0]
        health = "paused"
        message = (f"Vision model unreachable for {_fmt_age(now - s['since'])}: queue paused, "
                   f"retrying automatically.")
        if include_details and s.get("detail"):
            message += f" ({s['detail']})"
    elif stuck:
        s = stuck[0]
        health = "stalled"
        what = (s.get("job") or {}).get("name") if include_details else None
        message = (f"A worker has been {s['stage'] or 'busy'} for {_fmt_age(now - s['stage_since'])}"
                   + (f" on {what}" if what else "") + ". The supervisor requeues it after 15 min.")
    elif backoff:
        s = backoff[0]
        health = "degraded"
        wait = max(0, s.get("until", 0) - now)
        message = f"Connection trouble with Nextcloud: retrying in {_fmt_age(wait)}."
        if include_details and s.get("detail"):
            message += f" ({s['detail']})"
    elif processing:
        health = "working"
        message = f"Processing ({len(processing)} of {concurrency} worker{'s' if concurrency != 1 else ''} busy)."
    elif pending == 0:
        health = "idle"
        message = "Nothing to do: every queued file has been processed."
    elif last_done and now - last_done > NO_PROGRESS_SECONDS and now - STARTED_AT > NO_PROGRESS_SECONDS:
        health = "stalled"
        message = f"{pending} file(s) waiting but nothing completed for {_fmt_age(now - last_done)}."
    else:
        health = "working"
        message = "Picking up the next file."

    rate = throughput.get("done_last_hour", 0)
    eta = None
    if pending and throughput.get("done_last_15min"):
        per_sec = throughput["done_last_15min"] / 900
        eta = pending / per_sec

    out = {
        "health": health,
        "message": message,
        "now": now,
        "started_at": STARTED_AT,
        "workers_alive": workers_alive,
        "concurrency": concurrency,
        "last_done_ts": last_done or None,
        "done_last_15min": throughput.get("done_last_15min", 0),
        "done_last_hour": rate,
        "eta_seconds": eta,
    }
    if include_details:
        out["slots"] = [
            {
                "slot": s["slot"],
                "state": s["state"],
                "since": s["since"],
                "stage": s["stage"],
                "stage_since": s["stage_since"],
                "detail": s["detail"],
                "until": s["until"] or None,
                "stalled": s in stuck,
                "job": s.get("job"),
            }
            for s in slots
        ]
        out["events"] = events[:20]
    return out

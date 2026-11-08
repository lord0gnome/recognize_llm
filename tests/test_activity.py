"""Engine status verdicts: the dashboard must say working / idle / paused / stalled correctly."""

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
os.environ.setdefault("APP_ID", "recognize_llm")
os.environ.setdefault("APP_PERSISTENT_STORAGE", tempfile.mkdtemp())

import pytest  # noqa: E402

import activity  # noqa: E402
import job_queue  # noqa: E402

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def clean():
    activity._slots.clear()
    activity._events.clear()
    yield
    activity._slots.clear()
    activity._events.clear()


def verdict(pending=10, last_done=NOW - 30, alive=1, conc=1, details=True, **tp):
    throughput = {"last_done_ts": last_done, "done_last_15min": 30, "done_last_hour": 100, **tp}
    return activity.engine_status(
        counts={"pending": pending}, throughput=throughput, workers_alive=alive,
        concurrency=conc, include_details=details, request_timeout=180, now=NOW,
    )


def _at(slot, state, since, **kw):
    activity.set_slot(slot, state, **kw)
    activity._slots[slot]["since"] = since
    activity._slots[slot]["stage_since"] = since


def test_processing_is_working_with_eta():
    _at(0, activity.PROCESSING, NOW - 20, job={"user_id": "u", "file_id": 1})
    v = verdict()
    assert v["health"] == "working"
    assert v["eta_seconds"] == pytest.approx(10 / (30 / 900))
    assert v["slots"][0]["state"] == "processing" and not v["slots"][0]["stalled"]


def test_empty_queue_is_idle():
    _at(0, activity.IDLE, NOW - 600)
    assert verdict(pending=0)["health"] == "idle"


def test_vision_outage_is_paused_and_names_reason():
    _at(0, activity.WAITING_VISION, NOW - 120, detail="connection refused")
    v = verdict()
    assert v["health"] == "paused"
    assert "connection refused" in v["message"]
    assert "connection refused" not in verdict(details=False)["message"]


def test_long_running_stage_is_stalled():
    _at(0, activity.PROCESSING, NOW - 1000, job={"user_id": "u", "file_id": 1, "name": "a.jpg"})
    activity._slots[0]["stage"] = "downloading"
    v = verdict()
    assert v["health"] == "stalled" and "a.jpg" in v["message"]
    assert v["slots"][0]["stalled"] is True
    assert "a.jpg" not in verdict(details=False)["message"]


def test_captioning_gets_request_timeout_grace():
    _at(0, activity.PROCESSING, NOW - 400, job={"user_id": "u", "file_id": 1})
    activity._slots[0]["stage"] = "captioning"
    assert verdict()["health"] == "working"  # 400s < 300 + 180


def test_pending_but_no_progress_is_stalled(monkeypatch):
    monkeypatch.setattr(activity, "STARTED_AT", NOW - 7200)
    _at(0, activity.IDLE, NOW - 5)
    assert verdict(last_done=NOW - 3600)["health"] == "stalled"


def test_no_live_worker_is_stopped():
    assert verdict(alive=0)["health"] == "stopped"


def test_backoff_is_degraded():
    _at(0, activity.BACKOFF, NOW - 10, until=NOW + 50, detail="dns")
    v = verdict()
    assert v["health"] == "degraded" and "50s" in v["message"]


def test_non_admin_view_has_no_slots_or_events():
    activity.event("job user=alice file=1 error: boom")
    _at(0, activity.PROCESSING, NOW - 5, job={"user_id": "alice", "file_id": 1, "path": "/secret.jpg"})
    v = verdict(details=False)
    assert "slots" not in v and "events" not in v


def test_stage_updates_only_the_calling_workers_slot():
    activity.set_slot(3, activity.PROCESSING, job={"user_id": "u", "file_id": 9})
    activity.stage("captioning", name="x.jpg")  # main thread: not a worker, no-op
    assert activity._slots[3]["stage"] == ""

    def worker():
        activity.bind_slot(3)
        activity.stage("captioning", name="x.jpg")
    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert activity._slots[3]["stage"] == "captioning"
    assert activity._slots[3]["job"]["name"] == "x.jpg"


def test_throughput_and_engine_status_from_the_queue_db():
    job_queue.init_db()
    tp = job_queue.throughput()
    assert set(tp) == {"last_done_ts", "done_last_15min", "done_last_hour"}
    out = job_queue.engine_status(include_details=True)
    assert {"pending", "done", "engine"} <= set(out)
    assert out["engine"]["health"] in {"working", "idle", "paused", "degraded", "stalled", "stopped"}

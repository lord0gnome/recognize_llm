"""Tests for the never-stuck queue policy: transient-error classification and the
no-reuse Nextcloud transport patch. No network, no Nextcloud."""

import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
# job_queue resolves its DB path from ExApp persistent storage at import time.
# nc_py_api's persistent_storage() eagerly evaluates its fallback, which needs APP_ID too.
os.environ.setdefault("APP_ID", "recognize_llm")
os.environ.setdefault("APP_PERSISTENT_STORAGE", tempfile.mkdtemp())

from nc_py_api import NextcloudException  # noqa: E402

import job_queue  # noqa: E402
import nc_transport  # noqa: E402


def _nc_exc(status):
    return NextcloudException(status_code=status, reason="x")


def test_transient_statuses_are_transient():
    # 996/997/999 are nc_py_api's OCS pseudo-statuses for server-error/unauthorised/unknown.
    for status in (400, 401, 408, 429, 500, 502, 503, 504, 996, 997, 999):
        assert job_queue._is_transient_infra_error(_nc_exc(status)), status


def test_job_level_statuses_are_not_transient():
    # 998 is the OCS pseudo-status for not-found — the file's problem, not the network's.
    for status in (403, 404, 405, 409, 422, 998):
        assert not job_queue._is_transient_infra_error(_nc_exc(status)), status


def test_http_error_with_response_uses_its_status():
    class Resp:
        status_code = 502

    err = Exception("gateway")
    err.response = Resp()
    assert job_queue._is_transient_infra_error(err)

    Resp.status_code = 403
    assert not job_queue._is_transient_infra_error(err)


def test_raw_network_errors_are_transient():
    import socket

    assert job_queue._is_transient_infra_error(ConnectionError("boom"))
    assert job_queue._is_transient_infra_error(TimeoutError("slow"))
    assert job_queue._is_transient_infra_error(socket.gaierror("dns"))


def test_ordinary_errors_are_not_transient():
    assert not job_queue._is_transient_infra_error(ValueError("bad json"))
    assert not job_queue._is_transient_infra_error(KeyError("missing"))
    # Bare OSError is deliberately job-level: PIL raises OSError subclasses for corrupt media.
    assert not job_queue._is_transient_infra_error(OSError("weird"))
    assert not job_queue._is_transient_infra_error(FileNotFoundError("gone"))

    class FakePilError(OSError):
        pass

    FakePilError.__module__ = "PIL.Image"
    assert not job_queue._is_transient_infra_error(FakePilError("corrupt frame"))


def test_crash_orphan_burns_attempts_and_parks():
    """A job that kills the container (OOM poison pill) must not crash-loop forever:
    each startup reset burns an attempt, and MAX_ATTEMPTS crashes park it as failed."""
    job_queue.init_db()
    job_queue.enqueue("crash-test", 555555, source="manual")
    for expected_attempts in (1, 2, 3):
        row = job_queue._claim()
        while row is not None and row["file_id"] != 555555:
            row = job_queue._claim()  # skip unrelated rows other tests may have left pending
        assert row is not None and row["file_id"] == 555555
        job_queue.init_db()  # simulate container crash + restart while processing
        with job_queue._connect() as con:
            r = con.execute(
                "SELECT status, attempts FROM jobs WHERE file_id=555555").fetchone()
        assert r["attempts"] == expected_attempts
        assert r["status"] == ("failed" if expected_attempts >= 3 else "pending")


def test_stale_worker_writes_are_noops():
    """A worker whose attempt outlived the reaper must not clobber a re-claimed row."""
    job_queue.init_db()
    job_queue.enqueue("stale-test", 424242, source="manual")
    row = job_queue._claim()
    assert row is not None and row["claim_ts"]

    # Reaper intervenes: the job is requeued with a different stamp.
    with job_queue._connect() as con:
        con.execute(
            "UPDATE jobs SET status='pending', updated_at=updated_at + 999 WHERE file_id=424242"
        )

    job_queue._finish(row, "failed", "stale write")  # must be a no-op
    job_queue._requeue(row, "stale requeue")         # must be a no-op
    with job_queue._connect() as con:
        r = con.execute(
            "SELECT status, attempts, error FROM jobs WHERE file_id=424242"
        ).fetchone()
    assert (r["status"], r["attempts"], r["error"]) == ("pending", 0, "")

    # A fresh claim finishes normally and burns the attempt.
    row2 = job_queue._claim()
    assert row2 is not None and row2["file_id"] == 424242
    job_queue._finish(row2, "done")
    with job_queue._connect() as con:
        r = con.execute("SELECT status, attempts FROM jobs WHERE file_id=424242").fetchone()
    assert (r["status"], r["attempts"]) == ("done", 1)


def test_transport_patch_is_installed():
    import nc_py_api._session as ncs

    assert ncs.Session is nc_transport._OneShotSession


def test_one_shot_session_forces_h1_and_connection_close():
    captured = {}

    with mock.patch.object(
        nc_transport._ncs.Session.__mro__[1], "__init__", return_value=None
    ) as init:
        nc_transport._OneShotSession()
        assert init.call_args.kwargs["disable_http2"] is True
        assert init.call_args.kwargs["disable_http3"] is True

    session = nc_transport._OneShotSession.__new__(nc_transport._OneShotSession)
    with mock.patch.object(
        nc_transport._ncs.Session.__mro__[1], "request",
        side_effect=lambda method, url, *a, **kw: captured.update(kw) or None,
    ):
        session.request("GET", "http://example/")
    assert captured["headers"]["Connection"] == "close"

    # Caller-supplied Connection header wins.
    captured.clear()
    with mock.patch.object(
        nc_transport._ncs.Session.__mro__[1], "request",
        side_effect=lambda method, url, *a, **kw: captured.update(kw) or None,
    ):
        session.request("GET", "http://example/", headers={"Connection": "keep-alive"})
    assert captured["headers"]["Connection"] == "keep-alive"

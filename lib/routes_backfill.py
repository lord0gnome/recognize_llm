"""Admin-triggered, resumable backfill of existing image and video libraries.

Crawls each in-scope user's files for images and videos and enqueues them. The queue's per-file
etag marker makes re-runs cheap (already-processed, unchanged files are skipped by the worker).
"""

from __future__ import annotations

import threading
from typing import Annotated

import job_queue
import settings as settings_mod
from fastapi import APIRouter, BackgroundTasks, Depends
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import LogLvl, nc_app
from pydantic import BaseModel

router = APIRouter()

_lock = threading.Lock()
_state = {
    "running": False,
    "enqueued": 0,
    "users_total": 0,
    "users_done": 0,
    "current_user": "",
    "error": "",
}


class BackfillRequest(BaseModel):
    users: list[str] = []
    path: str = ""


def _resolve_users(nc: NextcloudApp) -> list[str]:
    try:
        users = nc.users.get_list()
        if users:
            return users
    except Exception:  # provisioning scope may be unavailable; fall back to the caller
        pass
    return [nc.user] if nc.user else []


def _crawl(nc: NextcloudApp, users: list[str], path: str) -> None:
    with _lock:
        _state.update(running=True, enqueued=0, users_total=len(users), users_done=0, current_user="", error="")
    try:
        cfg = settings_mod.load(nc)
        allowed_mimes = set(cfg.mimetypes) | set(cfg.video_mimetypes)
        for uid in users:
            if not _state["running"]:
                break
            _state["current_user"] = uid
            nc.set_user(uid)
            nodes = nc.files.listdir(path or "", depth=-1)
            # Mounted directories are received shares or external storage — skip their contents.
            # Only the mount-point directory itself carries the "M" permission flag, so we
            # collect those roots first and then exclude any file path that falls under them.
            mounted_roots = {
                node.user_path.rstrip("/") + "/"
                for node in nodes
                if node.is_dir and node.is_mounted
            }
            for node in nodes:
                if not _state["running"]:
                    break
                if node.is_dir or (node.info.mimetype or "").lower() not in allowed_mimes:
                    continue
                if mounted_roots and any(node.user_path.startswith(m) for m in mounted_roots):
                    continue
                job_queue.enqueue(uid, node.info.fileid, source="backfill")
                _state["enqueued"] += 1
            _state["users_done"] += 1
        nc.log(LogLvl.INFO, f"recognize_llm: backfill enqueued {_state['enqueued']} files")
    except Exception as e:
        _state["error"] = str(e)
        nc.log(LogLvl.ERROR, f"recognize_llm: backfill failed: {e}")
    finally:
        _state["running"] = False
        _state["current_user"] = ""


@router.post("/backfill/start")
def start(
    req: BackfillRequest,
    nc: Annotated[NextcloudApp, Depends(nc_app)],
    background_tasks: BackgroundTasks,
) -> dict:
    if _state["running"]:
        return {"status": "already_running", **_state}
    users = req.users or _resolve_users(nc)
    if not users:
        return {"status": "no_users", "detail": "could not resolve any users to scan"}
    background_tasks.add_task(_crawl, nc, users, req.path)
    return {"status": "started", "users": users, "path": req.path or "/"}


@router.post("/backfill/stop")
def stop() -> dict:
    _state["running"] = False
    return {"status": "stopping"}


@router.get("/backfill/status")
def status() -> dict:
    return {"crawl": dict(_state), "queue": job_queue.status()}


class OccBackfillRequest(BaseModel):
    occ: dict = {}


@router.post("/occ/backfill")
def occ_backfill(
    req: OccBackfillRequest,
    nc: Annotated[NextcloudApp, Depends(nc_app)],
    background_tasks: BackgroundTasks,
) -> dict:
    """OCC command callback: ``occ recognize_llm:backfill [--users alice,bob] [--path /Photos]``."""
    options = (req.occ.get("options") or {})
    raw_users = options.get("users") or ""
    path = options.get("path") or ""
    users = [u.strip() for u in raw_users.split(",") if u.strip()] if raw_users else _resolve_users(nc)
    if not users:
        return {"status": "no_users"}
    if _state["running"]:
        return {"status": "already_running", **_state}
    background_tasks.add_task(_crawl, nc, users, path)
    return {"status": "started", "users": users, "path": path or "/"}


@router.post("/occ/cluster-faces")
def occ_cluster_faces(
    req: OccBackfillRequest,
    nc: Annotated[NextcloudApp, Depends(nc_app)],
    background_tasks: BackgroundTasks,
) -> dict:
    """OCC callback: ``occ recognize_llm:cluster-faces [--users alice,bob] [--min-photos 3]``."""
    import face_pipeline
    import settings as settings_mod

    options = req.occ.get("options") or {}
    raw_users = options.get("users") or ""
    users = [u.strip() for u in raw_users.split(",") if u.strip()] if raw_users else _resolve_users(nc)
    if not users:
        return {"status": "no_users"}
    cfg = settings_mod.load(nc)
    min_samples = max(2, int(options.get("min-photos") or cfg.face_min_samples))
    background_tasks.add_task(face_pipeline.cluster_and_tag, nc, users, min_samples)
    return {"status": "started", "users": users, "min_photos": min_samples}

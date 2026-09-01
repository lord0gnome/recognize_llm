"""Admin endpoints for tag consolidation (ADMIN-gated by the info.xml catch-all route)."""

from __future__ import annotations

from typing import Annotated

import tag_merge
from fastapi import APIRouter, BackgroundTasks, Depends
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import nc_app
from pydantic import BaseModel

router = APIRouter()


class AnalyzeRequest(BaseModel):
    include_uppercase: bool = True


class RejectRequest(BaseModel):
    source: str
    rejected: bool = True


@router.post("/tags/merge/analyze")
def analyze(
    req: AnalyzeRequest,
    nc: Annotated[NextcloudApp, Depends(nc_app)],
    background_tasks: BackgroundTasks,
) -> dict:
    if tag_merge.state()["running"]:
        return {"status": "already_running", **tag_merge.state()}
    background_tasks.add_task(tag_merge._analyze, nc, req.include_uppercase)
    return {"status": "started"}


@router.post("/tags/merge/stop")
def stop() -> dict:
    tag_merge.request_stop()
    return {"status": "stopping"}


@router.get("/tags/merge/status")
def status() -> dict:
    run = tag_merge.latest_run() or {}
    return {
        **tag_merge.state(),
        "error": tag_merge.state()["error"] or run.get("error", ""),
        "phase": run.get("phase", "none"),
        "run_id": run.get("id", 0),
        "run_chunks_done": run.get("chunks_done", 0),
        "run_chunks_total": run.get("chunks_total", 0),
        "pair_counts": tag_merge.pair_counts(),
        "alias_count": len(tag_merge.get_alias_map()),
    }


@router.get("/tags/merge/proposal")
def proposal() -> dict:
    return tag_merge.get_proposal()


@router.post("/tags/merge/reject")
def reject(req: RejectRequest) -> dict:
    ok = tag_merge.set_pair_rejected(req.source, req.rejected)
    return {"status": "ok" if ok else "not_found"}


@router.post("/tags/merge/approve")
def approve() -> dict:
    return {"approved": tag_merge.approve_run()}


@router.post("/tags/merge/apply")
def apply(
    nc: Annotated[NextcloudApp, Depends(nc_app)],
    background_tasks: BackgroundTasks,
) -> dict:
    if tag_merge.state()["running"]:
        return {"status": "already_running", **tag_merge.state()}
    background_tasks.add_task(tag_merge._apply, nc)
    return {"status": "started"}


@router.post("/tags/merge/discard")
def discard() -> dict:
    return {"status": "discarded" if tag_merge.discard_run() else "nothing_to_discard"}


class OccRequest(BaseModel):
    occ: dict = {}


@router.post("/occ/consolidate-tags")
def occ_consolidate(
    req: OccRequest,
    nc: Annotated[NextcloudApp, Depends(nc_app)],
    background_tasks: BackgroundTasks,
) -> dict:
    """occ recognize_llm:consolidate-tags [--action=analyze|apply|status] [--include-uppercase yes]"""
    options = req.occ.get("options") or {}
    action = (options.get("action") or "analyze").strip().lower()
    if action == "status":
        return status()
    if tag_merge.state()["running"]:
        return {"status": "already_running", **tag_merge.state()}
    if action == "apply":
        background_tasks.add_task(tag_merge._apply, nc)
    else:
        include_upper = str(options.get("include-uppercase") or "yes").lower() in (
            "1", "yes", "true", "on")
        background_tasks.add_task(tag_merge._analyze, nc, include_upper)
    return {"status": "started", "action": action}

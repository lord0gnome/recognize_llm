"""Receives AppAPI file-system event notifications and enqueues image files."""

from __future__ import annotations

import job_queue
from fastapi import APIRouter, responses
from nc_py_api.ex_app.defs import FileSystemEventNotification

router = APIRouter()


@router.post("/events/node")
async def on_node_event(event: FileSystemEventNotification) -> responses.Response:
    target = event.event_data.target
    # Authoritative mimetype check happens in the processor; here we only cheaply skip non-images.
    if target.fileType.lower() == "file" and target.mime.lower().startswith("image/"):
        job_queue.enqueue(target.userId, target.fileId, source="event")
    return responses.Response()

"""Receives AppAPI file-system event notifications and enqueues image files."""

from __future__ import annotations

import os
import job_queue
from fastapi import APIRouter, responses
from nc_py_api.ex_app.defs import FileSystemEventNotification

router = APIRouter()

# NC sometimes sends application/octet-stream for freshly-uploaded files before mime detection runs.
# Fall back to extension so those uploads aren't silently dropped.
_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".webp",
    ".heic", ".heif", ".gif", ".tiff", ".tif",
    ".avif", ".bmp", ".raw", ".cr2", ".nef",
    ".arw", ".dng", ".orf",
}


@router.post("/events/node")
async def on_node_event(event: FileSystemEventNotification) -> responses.Response:
    target = event.event_data.target
    if target.fileType.lower() == "file":
        mime_ok = target.mime.lower().startswith("image/")
        ext_ok = os.path.splitext(target.name.lower())[1] in _IMAGE_EXTENSIONS
        if mime_ok or ext_ok:
            job_queue.enqueue(target.userId, target.fileId, source="event")
    return responses.Response()

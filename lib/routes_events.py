"""Receives AppAPI file-system event notifications and enqueues image and video files."""

from __future__ import annotations

import os
import job_queue
from fastapi import APIRouter, Request, responses

router = APIRouter()

# NC sometimes sends application/octet-stream for freshly-uploaded files before mime detection runs.
# Fall back to extension so those uploads aren't silently dropped.
_MEDIA_EXTENSIONS = {
    # images
    ".jpg", ".jpeg", ".jpe", ".png", ".webp",
    ".heic", ".heif", ".gif", ".tiff", ".tif",
    ".avif", ".bmp", ".raw", ".cr2", ".nef",
    ".arw", ".dng", ".orf",
    # videos
    ".mp4", ".m4v", ".mov", ".avi", ".mkv",
    ".webm", ".ogv", ".mpeg", ".mpg",
}


@router.post("/events/node")
async def on_node_event(request: Request) -> responses.Response:
    # Parse raw JSON — tolerates schema drift between NC/AppAPI versions (e.g. favorite: bool vs str).
    try:
        body = await request.json()
        target = body.get("event_data", {}).get("target", {})
        if target.get("fileType", "").lower() == "file":
            mime = (target.get("mime") or "").lower()
            name = target.get("name") or ""
            mime_ok = mime.startswith("image/") or mime.startswith("video/")
            ext_ok = os.path.splitext(name.lower())[1] in _MEDIA_EXTENSIONS
            if mime_ok or ext_ok:
                job_queue.enqueue(target["userId"], int(target["fileId"]), source="event")
    except Exception:
        pass
    return responses.Response()

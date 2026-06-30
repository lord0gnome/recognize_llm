"""The shared captioning engine: given a file, caption it and write metadata back.

Used by all three entry points (upload events, backfill, TaskProcessing provider).
"""

from __future__ import annotations

from dataclasses import dataclass

import face_pipeline
import storage
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import LogLvl
from settings import Settings
from vision_client import Caption, VisionClient


@dataclass
class Result:
    status: str  # "done" | "skipped" | "failed"
    caption: Caption | None = None
    reason: str = ""
    name: str = ""
    path: str = ""


def caption_bytes(image: bytes, mimetype: str, settings: Settings) -> Caption:
    """Caption raw bytes without touching Nextcloud (used by the TaskProcessing provider)."""
    return VisionClient(settings).caption(image, mimetype)


def process_file(nc: NextcloudApp, user_id: str, file_id: int, settings: Settings, force: bool = False) -> Result:
    """Caption a file owned by ``user_id`` and write tags + description back.

    Acting in the file owner's context is required for system tags and file access.
    Videos are handled by extracting a 3×3 frame grid and sending it to the LLM.
    After a successful caption, face embeddings are also extracted if enabled.
    """
    nc.set_user(user_id)
    node = nc.files.by_id(file_id)
    if node is None or node.is_dir:
        return Result("skipped", reason="not a file")

    mimetype = (node.info.mimetype or "").lower()
    is_video = settings.video_mimetype_allowed(mimetype)

    if not (settings.mimetype_allowed(mimetype) or is_video):
        return Result("skipped", reason=f"mimetype {mimetype}")
    if not force and storage.get_marker(nc, node) == node.etag:
        return Result("skipped", reason="unchanged (already processed)")

    nc.log(LogLvl.INFO, f"recognize_llm: captioning {node.user_path} (user={user_id})")
    raw_bytes = nc.files.download(node)

    if is_video:
        from video_utils import extract_frame_grid
        image_bytes = extract_frame_grid(raw_bytes)
    else:
        image_bytes = raw_bytes

    caption = VisionClient(settings).caption(image_bytes, mimetype, is_video=is_video)
    storage.write_results(nc, node, caption, settings)
    storage.set_marker(nc, node)
    nc.log(LogLvl.INFO, f"recognize_llm: tagged {node.user_path} with {caption.tags}")

    # Extract face embeddings from images (not videos) when clustering is enabled.
    if not is_video and settings.face_clustering:
        face_pipeline.extract_faces(raw_bytes, user_id, file_id)

    return Result("done", caption=caption, name=node.name, path=node.user_path)

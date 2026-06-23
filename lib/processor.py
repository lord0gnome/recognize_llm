"""The shared captioning engine: given a file, caption it and write metadata back.

Used by all three entry points (upload events, backfill, TaskProcessing provider).
"""

from __future__ import annotations

from dataclasses import dataclass

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
    """
    nc.set_user(user_id)
    node = nc.files.by_id(file_id)
    if node is None or node.is_dir:
        return Result("skipped", reason="not a file")
    if not settings.mimetype_allowed(node.info.mimetype):
        return Result("skipped", reason=f"mimetype {node.info.mimetype}")
    if not force and storage.get_marker(nc, node) == node.etag:
        return Result("skipped", reason="unchanged (already processed)")

    nc.log(LogLvl.INFO, f"recognize_llm: captioning {node.user_path} (user={user_id})")
    image = nc.files.download(node)
    caption = VisionClient(settings).caption(image, node.info.mimetype)
    storage.write_results(nc, node, caption, settings)
    storage.set_marker(nc, node)
    nc.log(LogLvl.INFO, f"recognize_llm: tagged {node.user_path} with {caption.tags}")
    return Result("done", caption=caption, name=node.name, path=node.user_path)

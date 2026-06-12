"""Write generated metadata back into Nextcloud.

- tags        -> Nextcloud system (collaborative) tags, searchable/filterable in Files
- description -> dead DAV property (machine-readable) + optional user-visible comment
- marker      -> dead DAV property holding the etag processed, so backfill skips unchanged files
"""

from __future__ import annotations

import dav
from nc_py_api import NextcloudApp, FsNode
from nc_py_api._exceptions import NextcloudExceptionNotFound
from nc_py_api.ex_app import LogLvl
from settings import Settings
from vision_client import Caption


def _ensure_tag(nc: NextcloudApp, name: str):
    try:
        return nc.files.tag_by_name(name)
    except NextcloudExceptionNotFound:
        nc.files.create_tag(name, user_visible=True, user_assignable=True)
        return nc.files.tag_by_name(name)


def write_results(nc: NextcloudApp, node: FsNode, caption: Caption, settings: Settings) -> None:
    for name in caption.tags[: settings.max_tags]:
        tag = _ensure_tag(nc, name)
        nc.files.assign_tag(node, tag)

    if caption.description:
        dav.set_props(nc, node, {dav.PROP_DESCRIPTION: caption.description})
        if settings.write_comment:
            try:
                dav.add_comment(nc, node.info.fileid, caption.description)
            except Exception as e:  # comments are best-effort, never fail the job over them
                nc.log(LogLvl.WARNING, f"recognize_llm: could not add comment to {node.user_path}: {e}")


def get_marker(nc: NextcloudApp, node: FsNode) -> str:
    return dav.get_props(nc, node, [dav.PROP_ETAG]).get(dav.PROP_ETAG, "")


def set_marker(nc: NextcloudApp, node: FsNode) -> None:
    dav.set_props(nc, node, {dav.PROP_ETAG: node.etag})

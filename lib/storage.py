"""Write generated metadata back into Nextcloud.

- tags        -> Nextcloud system (collaborative) tags, searchable/filterable in Files
- description -> dead DAV property (machine-readable) + optional user-visible comment
- marker      -> dead DAV property holding the etag processed, so backfill skips unchanged files
"""

from __future__ import annotations

import dav
from nc_py_api import NextcloudApp, FsNode
from nc_py_api._exceptions import NextcloudException, NextcloudExceptionNotFound
from nc_py_api.ex_app import LogLvl
from settings import Settings
from vision_client import Caption


def _ensure_tag(nc: NextcloudApp, name: str):
    name_lower = name.lower()
    tags = nc.files.list_tags()
    existing = next((t for t in tags if t.display_name.lower() == name_lower), None)
    if existing:
        return existing
    try:
        nc.files.create_tag(name, user_visible=True, user_assignable=True)
    except NextcloudException as e:
        if e.status_code != 409:
            raise
        # Race or case-insensitive conflict — refresh and look again
        tags = nc.files.list_tags()
        existing = next((t for t in tags if t.display_name.lower() == name_lower), None)
        if existing:
            return existing
        raise
    return nc.files.tag_by_name(name)


def write_results(nc: NextcloudApp, node: FsNode, caption: Caption, settings: Settings) -> None:
    # System tag creation requires admin privileges; non-admin users get a 403 on POST /systemtags.
    # Drop to app-level auth (empty user) for all tag operations, then restore the file owner so
    # the DAV property writes use the correct user-scoped path (/files/{owner}/…).
    file_owner = nc.user
    nc.set_user("")
    for name in caption.tags[: settings.max_tags]:
        tag = _ensure_tag(nc, name)
        try:
            nc.files.assign_tag(node, tag)
        except NextcloudException as e:
            if e.status_code != 409:  # 409 = already assigned, treat as idempotent
                raise
    nc.set_user(file_owner)

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

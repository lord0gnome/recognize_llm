"""Low-level WebDAV helpers for custom properties and comments.

nc_py_api exposes system tags and file ops, but not custom dead-properties or comments, so we talk
to the DAV adapter directly (the same ``_session.adapter_dav`` the library uses internally).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import quote
from xml.sax.saxutils import escape

from nc_py_api import NextcloudApp, FsNode
from nc_py_api.files._files import dav_get_obj_path

NS = "https://github.com/lord0gnome/recognize_llm/ns"
PROP_DESCRIPTION = "description"
PROP_ETAG = "processed-etag"


def _obj_path(nc: NextcloudApp, node: FsNode) -> str:
    return quote(dav_get_obj_path(nc.user, node.user_path))


def set_props(nc: NextcloudApp, node: FsNode, props: dict[str, str]) -> None:
    """PROPPATCH a set of dead properties in our namespace onto a file."""
    sets = "".join(f"<rl:{k}>{escape(v)}</rl:{k}>" for k, v in props.items())
    body = (
        '<?xml version="1.0"?>'
        f'<d:propertyupdate xmlns:d="DAV:" xmlns:rl="{NS}">'
        f"<d:set><d:prop>{sets}</d:prop></d:set>"
        "</d:propertyupdate>"
    )
    resp = nc._session.adapter_dav.request("PROPPATCH", _obj_path(nc, node), data=body)
    resp.raise_for_status()


def get_props(nc: NextcloudApp, node: FsNode, names: list[str]) -> dict[str, str]:
    """PROPFIND our dead properties; returns only those that are present and non-empty."""
    requested = "".join(f"<rl:{n}/>" for n in names)
    body = (
        '<?xml version="1.0"?>'
        f'<d:propfind xmlns:d="DAV:" xmlns:rl="{NS}"><d:prop>{requested}</d:prop></d:propfind>'
    )
    resp = nc._session.adapter_dav.request(
        "PROPFIND", _obj_path(nc, node), data=body, headers={"Depth": "0"}
    )
    resp.raise_for_status()
    out: dict[str, str] = {}
    root = ET.fromstring(resp.text)
    for name in names:
        for el in root.iter(f"{{{NS}}}{name}"):
            if el.text:
                out[name] = el.text
                break
    return out


def add_comment(nc: NextcloudApp, file_id: int, message: str) -> None:
    """Attach a user-visible comment to a file (best-effort)."""
    resp = nc._session.adapter_dav.post(
        f"/comments/files/{file_id}",
        json={"actorType": "users", "actorId": nc.user or "admin", "verb": "comment", "message": message},
    )
    resp.raise_for_status()

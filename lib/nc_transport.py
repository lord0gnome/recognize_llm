"""Immunize every Nextcloud HTTP request against keep-alive connection-reuse races.

Why this exists (the "queue frozen, restart fixes it" trap — see OPERATIONS.md):
ExApp->NC traffic traverses a reverse proxy whose upstream closes idle connections after ~5s
(Apache KeepAliveTimeout). A request written onto a connection the server just closed surfaces as
a bogus 400/502 mid-job. Healthy jobs idle 60-90s inside the llama call, so stale connections are
evicted naturally — but a streak of fast failures tightens the loop cadence into the danger window
and every request dies, freezing the queue until a container restart.

Fix: force HTTP/1.1 (a `Connection` header is meaningless — and stripped — on h2/h3) and ask the
server to close the connection after every response. No reuse, no race. The overhead (one TCP+TLS
handshake per request at ~1 request every few seconds) is negligible for this workload.

Import this module BEFORE anything constructs a NextcloudApp (done at the top of lib/main.py).
nc_py_api resolves the name `Session` inside nc_py_api._session at call time, and only passes
base_url / timeout / keepalive_delay / pool_maxsize to it, so the subclass kwargs never conflict.
NcSessionApp replaces `session.headers` wholesale after construction, which is why the header is
injected per-request in request() rather than set on the session. The async sessions built by
AppAPIAuthMiddleware never send HTTP (signature check is local), so patching the sync Session
covers all real Nextcloud traffic. lib/vision_client.py uses httpx and is unaffected.
"""

from __future__ import annotations

import nc_py_api._session as _ncs


class _OneShotSession(_ncs.Session):  # niquests.Session
    """HTTP/1.1-only session that tells the server to close the connection after every response."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("disable_http2", True)
        kwargs.setdefault("disable_http3", True)
        super().__init__(*args, **kwargs)

    def request(self, method, url, *args, **kwargs):
        headers = dict(kwargs.get("headers") or {})
        headers.setdefault("Connection", "close")
        kwargs["headers"] = headers
        return super().request(method, url, *args, **kwargs)


def install() -> None:
    """Idempotently swap nc_py_api's Session for the no-reuse variant."""
    if _ncs.Session is not _OneShotSession:
        _ncs.Session = _OneShotSession


install()

"""New-file detection that does NOT depend on AppAPI internals.

AppAPI 33/34 ship **no** file-system-event API: the ``events_listener`` added in AppAPI 2.4.0 was
dropped in the 33.x rewrite (the developer docs still describe it, but no controller/listener is
shipped — verified against stable33/stable34/v34.0.0/main). Patching it back into app_api's core
files works but is silently wiped by every ``occ app:update app_api``, which is exactly how upload
detection kept breaking. So we detect new uploads two update-proof ways instead:

  1. **NC core webhooks** (``webhook_listeners``, first-party since NC 30) — NC POSTs
     ``NodeCreatedEvent``/``NodeWrittenEvent`` to our ``/events/webhook``. Instant. The exApp
     registers them itself through the admin-less ``#[AppApiAdminAccessWithoutUser]`` create
     endpoint; NC records our app id as the owner, so a single ``DELETE .../byappid/<app>`` is a
     clean teardown/idempotent reset.
  2. **Periodic backstop scan** (:class:`PollLoop`) — every ``poll_interval`` we ask each known
     user's DAV for files modified since a stored high-water mark and enqueue them. Depends on
     nothing outside the exApp, so detection keeps working even if the webhook app is disabled or
     ``WEBHOOK_EXAPP_URL`` is wrong.

Both paths feed the same :mod:`job_queue`; the queue's ``(user_id, file_id)`` key plus the per-file
etag marker make duplicate deliveries between the two paths (and repeated webhook writes) free.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

import job_queue
import settings as settings_mod
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import LogLvl

_APP_ID = os.environ.get("APP_ID", "recognize_llm")

# Receiver route (also listed in AppAPIAuthMiddleware's disable_for — webhooks carry our own shared
# secret, not AppAPI's request signature) and the header the secret travels in.
WEBHOOK_PATH = "/events/webhook"
WEBHOOK_SECRET_HEADER = "X-Recognize-Secret"

# NC core webhook_listeners OCS API (v2). create() is #[AppApiAdminAccessWithoutUser], so the exApp
# may call it with no user session; byappid delete lets us reset our own webhooks idempotently.
_WH_API = "/ocs/v2.php/apps/webhook_listeners/api/v1/webhooks"
_NODE_EVENTS = (
    "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
    "OCP\\Files\\Events\\Node\\NodeWrittenEvent",
)

# NC sometimes reports application/octet-stream for freshly-uploaded files before mime detection
# runs, and webhook payloads carry no mime at all — so fall back to the extension.
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

# Internal node path is /<userId>/files[/rel/path]; anything else (appdata, previews, trashbin,
# versions, __groupfolders roots without a user, …) is not a user-visible file we caption.
_USER_PATH_RE = re.compile(r"^/([^/]+)/files(/.*)?$")


def _log(nc, level, msg: str) -> None:
    try:
        nc.log(level, f"recognize_llm: {msg}")
    except Exception:
        pass


def is_media(name: str, mime: str = "") -> bool:
    """True if a file looks like an image/video by mime or, failing that, by extension."""
    mime = (mime or "").lower()
    if mime.startswith("image/") or mime.startswith("video/"):
        return True
    return os.path.splitext((name or "").lower())[1] in _MEDIA_EXTENSIONS


# ── Enqueue helpers (shared by the webhook receiver and the legacy AppAPI-event route) ──────────

def enqueue_target(target: dict, source: str) -> bool:
    """Enqueue from an AppAPI-event ``target`` dict (mime/name/userId/fileId all present)."""
    if (target.get("fileType", "").lower() or "file") != "file":
        return False
    if not is_media(target.get("name") or "", target.get("mime") or ""):
        return False
    try:
        job_queue.enqueue(target["userId"], int(target["fileId"]), source=source)
    except (KeyError, TypeError, ValueError):
        return False
    return True


def enqueue_from_webhook(body: dict) -> bool:
    """Enqueue from a core webhook_listeners payload.

    Shape: ``{"event": {"class": ..., "node": {"id": int, "path": "/<user>/files/..."}}, ...}``.
    The payload carries no mime, so the media check is extension-only here; the worker re-checks the
    real mimetype and skips anything that isn't allowed.
    """
    node = (body.get("event") or {}).get("node") or {}
    file_id = node.get("id")
    m = _USER_PATH_RE.match(node.get("path") or "")
    if file_id is None or not m:
        return False
    name = os.path.basename((m.group(2) or "").rstrip("/"))
    if not name or not is_media(name):
        return False
    try:
        job_queue.enqueue(m.group(1), int(file_id), source="webhook")
    except (TypeError, ValueError):
        return False
    return True


# ── Webhook registration (idempotent, owned by our app id) ──────────────────────────────────────

# In-process cache of the shared secret so the (chatty) webhook receiver verifies each request
# without a settings round-trip to NC. Populated on register and lazily on first receipt.
_secret_cache: str | None = None


def ensure_secret(nc, cfg) -> str:
    """Return the shared webhook secret, generating + persisting one on first use."""
    global _secret_cache
    secret = cfg.webhook_secret
    if not secret:
        secret = secrets.token_urlsafe(32)
        try:
            nc.appconfig_ex.set_value("webhook_secret", secret, sensitive=True)
        except Exception as e:
            _log(nc, LogLvl.WARNING, f"could not persist webhook secret: {e!r}")
    _secret_cache = secret
    return secret


def cached_secret() -> str | None:
    """The webhook secret, loaded from config once and cached (survives restarts where the enable
    handler doesn't re-run). Returns None if none is configured."""
    global _secret_cache
    if _secret_cache is None:
        try:
            _secret_cache = settings_mod.load(NextcloudApp()).webhook_secret or None
        except Exception:
            return None
    return _secret_cache


def register_webhooks(nc, cfg) -> None:
    """(Re)register the NC core webhooks for upload detection. Never raises — on any failure we log
    and lean on the periodic backstop scan so app-enable still succeeds."""
    if not cfg.webhook_enabled:
        _log(nc, LogLvl.INFO, "webhooks disabled by config — using the periodic backstop scan only")
        return
    base = cfg.webhook_exapp_url
    if not base:
        _log(nc, LogLvl.WARNING,
             "WEBHOOK_EXAPP_URL is unset — skipping webhook registration (instant detection off; "
             "the periodic scan still covers new files). Set it to the URL Nextcloud uses to reach "
             "this exApp, e.g. http://<host>:<port>")
        return
    secret = ensure_secret(nc, cfg)
    uri = base + WEBHOOK_PATH
    unregister_webhooks(nc)  # drop any we registered before, so re-enable doesn't pile up duplicates
    created = 0
    for event in _NODE_EVENTS:
        try:
            nc.ocs("POST", _WH_API, json={
                "httpMethod": "POST",
                "uri": uri,
                "event": event,
                "authMethod": "header",
                "authData": {WEBHOOK_SECRET_HEADER: secret},
            })
            created += 1
        except Exception as e:
            _log(nc, LogLvl.ERROR,
                 f"webhook registration failed for {event}: {e!r} — is the 'webhook_listeners' app "
                 f"enabled? (occ app:enable webhook_listeners). Falling back to the periodic scan.")
    if created:
        _log(nc, LogLvl.INFO, f"registered {created} core webhook(s) → {uri}")


def unregister_webhooks(nc) -> None:
    """Remove every webhook this exApp registered (best-effort)."""
    try:
        nc.ocs("DELETE", f"{_WH_API}/byappid/{_APP_ID}")
    except Exception:
        pass


# ── Backstop poll loop ──────────────────────────────────────────────────────────────────────────

def _resolve_all_users(nc) -> list[str]:
    """Users to scan: everyone we've already seen (from our own DB, always available) unioned with
    the provisioning list when the scope allows it. Never needs a user session to return something."""
    users = set(job_queue.known_user_ids())
    try:
        provisioned = nc.users.get_list()
        if provisioned:
            users.update(provisioned)
    except Exception:
        pass  # provisioning scope unavailable — DB-known users are enough for a backstop
    return sorted(users)


class PollLoop:
    """Single daemon thread that periodically enqueues each user's newly-modified media files."""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="recognize-llm-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        if self._stop.wait(60):  # let startup settle before the first scan
            return
        nc = NextcloudApp()
        while not self._stop.is_set():
            interval = 900
            try:
                cfg = settings_mod.load(nc)
                interval = cfg.poll_interval
                if cfg.poll_enabled:
                    self._scan_once(nc, cfg)
            except Exception as e:
                _log(nc, LogLvl.WARNING, f"backstop scan error (recovered): {e!r}")
            if self._stop.wait(max(60, interval)):
                return

    def _scan_once(self, nc, cfg) -> None:
        allowed_mimes = set(cfg.mimetypes) | set(cfg.video_mimetypes)
        for uid in _resolve_all_users(nc):
            if self._stop.is_set():
                return
            try:
                self._scan_user(nc, uid, allowed_mimes)
            except Exception as e:
                _log(nc, LogLvl.WARNING, f"backstop scan failed for {uid}: {e!r}")

    def _scan_user(self, nc, uid: str, allowed_mimes: set[str]) -> None:
        nc.set_user(uid)
        watermark = job_queue.get_scan_watermark(uid)
        if watermark is None:
            # First sighting: seed the mark at "now" and DON'T enqueue the whole existing library
            # (that's what backfill is for) — we only detect files uploaded from here on.
            job_queue.set_scan_watermark(uid, int(time.time()))
            return
        max_mtime = watermark
        enqueued = 0
        for node in self._recent_nodes(nc, watermark):
            if node.is_dir:
                continue
            mtime = int(node.info.last_modified.timestamp())
            if mtime <= watermark:
                continue
            max_mtime = max(max_mtime, mtime)
            if (node.info.mimetype or "").lower() in allowed_mimes:
                job_queue.enqueue(uid, node.info.fileid, source="poll")
                enqueued += 1
        if max_mtime > watermark:
            job_queue.set_scan_watermark(uid, max_mtime)
        if enqueued:
            _log(nc, LogLvl.INFO, f"backstop scan enqueued {enqueued} new file(s) for {uid}")

    def _recent_nodes(self, nc, watermark: int):
        """Files modified after *watermark*. Prefer a cheap DAV SEARCH; fall back to a full walk if
        the server rejects the search (older/edge DAV), since the mtime gate above stays correct."""
        cutoff = datetime.fromtimestamp(watermark, tz=timezone.utc)
        try:
            return nc.files.find(["gt", "last_modified", cutoff])
        except Exception:
            return nc.files.listdir("", depth=-1)

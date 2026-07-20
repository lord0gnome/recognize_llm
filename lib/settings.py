"""Runtime configuration for recognize_llm.

Settings live in Nextcloud's app-config (``oc_appconfig_ex``) so an admin can edit them from the
settings UI, with environment variables as the initial defaults (handy for ``register --env``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from nc_py_api import NextcloudApp

DEFAULT_VIDEO_PROMPT_PREFIX = """\
This is a video represented as a 3×3 grid of frames sampled from start to end (left-to-right, top-to-bottom). """

DEFAULT_PROMPT = """\
You are a photo library metadata assistant. Examine the image and respond with ONLY a JSON object — no markdown, no text outside the JSON.

{"description": string, "tags": string[]}

"description": 1–3 factual sentences. Cover: the main subjects (for people note apparent roles or relationships like "a couple" or "two children" — never name individuals), the setting or location (room type, landscape, or any recognizable landmark or city), the activity or occasion if applicable, and any notable objects, text, or distinctive details visible in the image.

"tags": 6–12 lowercase keywords optimised for searching a personal photo library. Draw from multiple dimensions:
- Subjects: people, animals, objects (e.g. "dog", "birthday cake", "elderly man", "baby")
- Setting: location and environment (e.g. "beach", "kitchen", "mountain trail", "eiffel tower", "forest")
- Activity or occasion: what is happening (e.g. "hiking", "wedding", "cooking", "graduation", "concert")
- Visual attributes: style or conditions (e.g. "aerial", "close-up", "night", "black and white", "fog")
- Time of day or season when clearly visible (e.g. "sunset", "winter", "golden hour", "autumn")

Tags must be lowercase. Do not output anything outside the JSON object.\
"""

# Keys as stored in appconfig_ex, paired with their env-var fallback and built-in default.
_KEYS: dict[str, tuple[str, str]] = {
    # Base URL of the OpenAI-compatible endpoint. May or may not already include the `/v1` suffix
    # (Ollama is typically `http://host:11434/v1`; raw llama.cpp is typically `http://host:8080`).
    # host.containers.internal: the llama server runs on the same host as this container.
    # The host's LAN IP does NOT work from inside a bridge-networked rootless container
    # (pasta-published ports refuse hairpin connections); this alias always resolves.
    "llama_url": ("LLAMA_URL", "http://host.containers.internal:11434/v1"),
    # Required for Ollama; for single-model llama.cpp servers it may be left empty.
    "llama_model": ("LLAMA_MODEL", ""),
    "api_key": ("LLAMA_API_KEY", ""),
    "prompt": ("LLAMA_PROMPT", DEFAULT_PROMPT),
    "max_tags": ("MAX_TAGS", "8"),
    "mimetypes": ("MIMETYPES", "image/jpeg,image/png,image/webp,image/heic,image/heif,image/gif,image/tiff"),
    "video_mimetypes": ("VIDEO_MIMETYPES", "video/mp4,video/mpeg,video/quicktime,video/x-msvideo,video/x-matroska,video/webm,video/ogg"),
    "concurrency": ("CONCURRENCY", "1"),
    "request_timeout": ("REQUEST_TIMEOUT", "180"),
    "write_comment": ("WRITE_COMMENT", "yes"),
    "max_tokens": ("MAX_TOKENS", "1024"),
    "face_clustering": ("FACE_CLUSTERING", "yes"),
    "face_min_samples": ("FACE_MIN_SAMPLES", "3"),
    # Cosine similarity (0–1) required to fold a face into an existing person, both for real-time
    # matching on upload and as the DBSCAN neighbourhood (eps = 1 − this). Higher = stricter/purer.
    "face_match_min_similarity": ("FACE_MATCH_MIN_SIMILARITY", "0.5"),
    # GPS → place names: adds location tags and gives the model geographic context.
    "geotag": ("GEOTAG", "yes"),
    # Reverse-geocoding endpoint. Default is the public OSM instance (1 req/s, results
    # cached forever in the queue DB); point at a self-hosted Nominatim to keep photo
    # coordinates entirely local.
    "nominatim_url": ("NOMINATIM_URL", "https://nominatim.openstreetmap.org"),
    # Overpass endpoint for the nearest-landmark lookup (reverse geocoding alone can't
    # name a landmark the photo was taken NEAR). Empty disables landmark search.
    # No settings-UI field — configurable via env/appconfig for self-hosters.
    "overpass_url": ("OVERPASS_URL", "https://overpass-api.de/api/interpreter"),
    # ── New-file detection (see lib/file_events.py) ──────────────────────────────
    # AppAPI 33/34 ship no file-event API (the old events_listener was dropped in the
    # rewrite), so we detect uploads two update-proof ways instead: NC core webhooks
    # (instant) + a periodic self-scan (guaranteed-eventual backstop).
    "webhook_enabled": ("WEBHOOK_ENABLED", "yes"),
    # NC-reachable base URL of THIS exApp (no trailing /events/webhook). NC's core
    # webhook_listeners POSTs upload events here. For a manual_install daemon this is the
    # LAN address+port NC uses to reach the container, e.g. http://192.168.0.143:23000.
    # Empty ⇒ webhook registration is skipped (polling still covers detection).
    "webhook_exapp_url": ("WEBHOOK_EXAPP_URL", ""),
    # Shared secret sent by NC in a header and verified on receipt. Auto-generated and
    # persisted on first enable when left empty.
    "webhook_secret": ("WEBHOOK_SECRET", ""),
    # Periodic backstop scan: catches anything the webhook missed (webhook app disabled,
    # exApp unreachable, NC restart). Depends on nothing outside the exApp.
    "poll_enabled": ("POLL_ENABLED", "yes"),
    "poll_interval": ("POLL_INTERVAL", "900"),
}


@dataclass
class Settings:
    llama_url: str
    llama_model: str
    api_key: str
    prompt: str
    max_tags: int
    mimetypes: list[str]
    video_mimetypes: list[str]
    concurrency: int
    request_timeout: int
    write_comment: bool
    max_tokens: int
    face_clustering: bool
    face_min_samples: int
    face_match_min_similarity: float
    geotag: bool
    nominatim_url: str
    overpass_url: str
    webhook_enabled: bool
    webhook_exapp_url: str
    webhook_secret: str
    poll_enabled: bool
    poll_interval: int

    @property
    def chat_url(self) -> str:
        """Full chat-completions URL, tolerant of a base that already includes `/v1`."""
        base = self.llama_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    def mimetype_allowed(self, mimetype: str) -> bool:
        return bool(mimetype) and mimetype.lower() in self.mimetypes

    def video_mimetype_allowed(self, mimetype: str) -> bool:
        return bool(mimetype) and mimetype.lower() in self.video_mimetypes


def _raw(nc: NextcloudApp) -> dict[str, str]:
    """Resolve every key: appconfig_ex value if set, else env var, else built-in default."""
    stored = {r.key: r.value for r in nc.appconfig_ex.get_values(list(_KEYS))}
    out: dict[str, str] = {}
    for key, (env_var, default) in _KEYS.items():
        value = stored.get(key)
        if value is None or value == "":
            value = os.environ.get(env_var, default)
        out[key] = value
    return out


def load(nc: NextcloudApp) -> Settings:
    r = _raw(nc)
    return Settings(
        llama_url=r["llama_url"],
        llama_model=r["llama_model"],
        api_key=r["api_key"],
        prompt=r["prompt"],
        max_tags=max(1, int(r["max_tags"] or 8)),
        mimetypes=[m.strip().lower() for m in r["mimetypes"].split(",") if m.strip()],
        video_mimetypes=[m.strip().lower() for m in r["video_mimetypes"].split(",") if m.strip()],
        concurrency=max(1, int(r["concurrency"] or 1)),
        request_timeout=max(10, int(r["request_timeout"] or 180)),
        write_comment=str(r["write_comment"]).lower() in ("1", "yes", "true", "on"),
        max_tokens=max(64, int(r["max_tokens"] or 1024)),
        face_clustering=str(r["face_clustering"]).lower() in ("1", "yes", "true", "on"),
        face_min_samples=max(2, int(r["face_min_samples"] or 3)),
        face_match_min_similarity=min(0.95, max(0.1, float(r["face_match_min_similarity"] or 0.5))),
        geotag=str(r["geotag"]).lower() in ("1", "yes", "true", "on"),
        nominatim_url=r["nominatim_url"].strip() or "https://nominatim.openstreetmap.org",
        overpass_url=r["overpass_url"].strip(),
        webhook_enabled=str(r["webhook_enabled"]).lower() in ("1", "yes", "true", "on"),
        webhook_exapp_url=r["webhook_exapp_url"].strip().rstrip("/"),
        webhook_secret=r["webhook_secret"].strip(),
        poll_enabled=str(r["poll_enabled"]).lower() in ("1", "yes", "true", "on"),
        poll_interval=max(60, int(r["poll_interval"] or 900)),
    )

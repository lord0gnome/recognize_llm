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
    "llama_url": ("LLAMA_URL", "http://192.168.0.143:11434/v1"),
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
    )

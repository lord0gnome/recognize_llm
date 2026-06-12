"""Runtime configuration for recognize_llm.

Settings live in Nextcloud's app-config (``oc_appconfig_ex``) so an admin can edit them from the
settings UI, with environment variables as the initial defaults (handy for ``register --env``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from nc_py_api import NextcloudApp

DEFAULT_PROMPT = (
    "You are an image-tagging assistant. Look at the image and respond with ONLY a JSON object of "
    'the form {"description": string, "tags": string[]}. '
    '"description" is one or two factual sentences describing what is visible. '
    '"tags" is 5-10 short lowercase keywords for the main objects, scene, setting and notable '
    "attributes. Do not include any text outside the JSON object."
)

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
    "concurrency": ("CONCURRENCY", "1"),
    "request_timeout": ("REQUEST_TIMEOUT", "180"),
    "write_comment": ("WRITE_COMMENT", "yes"),
}


@dataclass
class Settings:
    llama_url: str
    llama_model: str
    api_key: str
    prompt: str
    max_tags: int
    mimetypes: list[str]
    concurrency: int
    request_timeout: int
    write_comment: bool

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
        concurrency=max(1, int(r["concurrency"] or 1)),
        request_timeout=max(10, int(r["request_timeout"] or 180)),
        write_comment=str(r["write_comment"]).lower() in ("1", "yes", "true", "on"),
    )

"""Client for a local llama.cpp (OpenAI-compatible) vision endpoint."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field

import httpx

from settings import Settings


@dataclass
class Caption:
    description: str = ""
    tags: list[str] = field(default_factory=list)


class VisionError(RuntimeError):
    """Raised when the vision endpoint fails or returns unusable output."""


def _data_url(image: bytes, mimetype: str) -> str:
    mt = mimetype if (mimetype or "").startswith("image/") else "image/jpeg"
    return f"data:{mt};base64,{base64.b64encode(image).decode('ascii')}"


def _extract_json(content: str) -> dict:
    """Parse the model's reply as JSON, tolerating stray prose or markdown fences."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    raise VisionError(f"Model did not return parseable JSON: {content[:200]!r}")


def _normalize_tags(raw, max_tags: int) -> list[str]:
    if isinstance(raw, str):
        raw = re.split(r"[,;\n]", raw)
    tags: list[str] = []
    seen: set[str] = set()
    for item in raw or []:
        tag = str(item).strip().lower().strip("#.")
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
        if len(tags) >= max_tags:
            break
    return tags


class VisionClient:
    def __init__(self, settings: Settings):
        self._s = settings

    def caption(self, image: bytes, mimetype: str) -> Caption:
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._s.prompt},
                        {"type": "image_url", "image_url": {"url": _data_url(image, mimetype)}},
                    ],
                }
            ],
            "temperature": 0.2,
            "max_tokens": 512,
            "response_format": {"type": "json_object"},
        }
        if self._s.llama_model:
            payload["model"] = self._s.llama_model

        headers = {"Content-Type": "application/json"}
        if self._s.api_key:
            headers["Authorization"] = f"Bearer {self._s.api_key}"

        try:
            resp = httpx.post(
                self._s.chat_url, json=payload, headers=headers, timeout=self._s.request_timeout
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise VisionError(f"Vision request to {self._s.chat_url} failed: {e}") from e

        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as e:
            raise VisionError(f"Unexpected vision response shape: {e}") from e

        data = _extract_json(content)
        return Caption(
            description=str(data.get("description", "")).strip(),
            tags=_normalize_tags(data.get("tags"), self._s.max_tags),
        )

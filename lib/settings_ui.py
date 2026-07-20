"""Admin settings form (declarative settings).

AppAPI persists each field's value into the exApp's ``appconfig_ex`` keyed by the field ``id``, so
the field ids here are exactly the keys read back in :mod:`settings`. No save endpoint is needed.
"""

from __future__ import annotations

from nc_py_api import NextcloudApp
from nc_py_api.ex_app import SettingsField, SettingsFieldType, SettingsForm
from settings import DEFAULT_PROMPT

FORM_ID = "recognize_llm_settings"


def _form() -> SettingsForm:
    return SettingsForm(
        id=FORM_ID,
        section_type="admin",
        section_id="additional",
        title="Recognize LLM",
        description=(
            "Tag and describe images using a local OpenAI-compatible vision model. "
            "Run backfill from the server: occ recognize_llm:backfill"
        ),
        fields=[
            # ── Vision endpoint ──────────────────────────────────────────────
            SettingsField(
                id="llama_url",
                title="Vision endpoint URL",
                type=SettingsFieldType.URL,
                default="http://192.168.0.143:11434/v1",
                placeholder="http://host:port/v1",
                description=(
                    "OpenAI-compatible base URL. "
                    "Must be reachable from the exApp container (not just your browser). "
                    "Ollama: http://host:11434/v1 — "
                    "llama.cpp server: http://host:8080 (the /v1 suffix is appended if missing)."
                ),
            ),
            SettingsField(
                id="llama_model",
                title="Model name",
                type=SettingsFieldType.TEXT,
                default="",
                placeholder="(optional for single-model servers)",
                description=(
                    "Required for Ollama and multi-model endpoints (e.g. \"gemma3:27b-it-q4\"). "
                    "Leave empty if the server only hosts one model."
                ),
            ),
            SettingsField(
                id="api_key",
                title="API key",
                type=SettingsFieldType.PASSWORD,
                default="",
                description="Bearer token sent with every request. Leave empty if your endpoint requires no authentication.",
                sensitive=True,
            ),
            SettingsField(
                id="max_tokens",
                title="Max response tokens",
                type=SettingsFieldType.NUMBER,
                default=1024,
                description=(
                    "Token budget for the model's JSON reply. "
                    "512 is often too small — a detailed description with 10 tags can exceed it, "
                    "cutting the JSON mid-object and failing the job. 1024 is recommended."
                ),
            ),
            # ── Captioning behaviour ─────────────────────────────────────────
            SettingsField(
                id="prompt",
                title="Prompt",
                type=SettingsFieldType.TEXT,
                default=DEFAULT_PROMPT,
                description=(
                    "Instruction sent to the model alongside each image. "
                    "Must ask for a JSON object with a \"description\" string and a \"tags\" string array — "
                    "those are the only two fields the app reads. "
                    "Changing this takes effect immediately for the next processed image."
                ),
            ),
            SettingsField(
                id="max_tags",
                title="Max tags per image",
                type=SettingsFieldType.NUMBER,
                default=8,
                description=(
                    "Only the first N tags from the model's response are written to Nextcloud as system tags. "
                    "The full list is still stored in the description DAV property."
                ),
            ),
            # ── File selection ───────────────────────────────────────────────
            SettingsField(
                id="mimetypes",
                title="Image types to process",
                type=SettingsFieldType.TEXT,
                default="image/jpeg,image/png,image/webp,image/heic,image/heif,image/gif,image/tiff",
                description=(
                    "Comma-separated MIME type allowlist. "
                    "Files whose type is not in this list are silently skipped. "
                    "Common types: image/jpeg, image/png, image/webp, image/heic, image/heif."
                ),
            ),
            # ── Workers ──────────────────────────────────────────────────────
            SettingsField(
                id="concurrency",
                title="Worker concurrency",
                type=SettingsFieldType.NUMBER,
                default=1,
                description=(
                    "Number of images captioned in parallel. "
                    "Keep at 1 unless your vision endpoint handles concurrent requests well. "
                    "Increasing this takes effect after disabling and re-enabling the app."
                ),
            ),
            SettingsField(
                id="request_timeout",
                title="Request timeout (seconds)",
                type=SettingsFieldType.NUMBER,
                default=180,
                description=(
                    "Seconds to wait for the vision endpoint per image before declaring a failure. "
                    "Increase for large images, slow hardware, or quantised models on CPU."
                ),
            ),
            # ── Output ───────────────────────────────────────────────────────
            SettingsField(
                id="write_comment",
                title="Write description as a file comment",
                type=SettingsFieldType.CHECKBOX,
                default=True,
                description=(
                    "Adds the generated description as a visible comment in Files → Comments tab. "
                    "The description is always stored as a DAV dead-property (machine-readable) "
                    "regardless of this setting."
                ),
            ),
            # ── People / face grouping (M7) ──────────────────────────────────
            SettingsField(
                id="face_clustering",
                title="Group faces into people",
                type=SettingsFieldType.CHECKBOX,
                default=True,
                description=(
                    "Detect faces in processed photos and group them into people, tagged person:… and "
                    "reviewable under the People menu. Runs fully locally (InsightFace/ArcFace) — face "
                    "data is never sent to the vision endpoint. Videos are never face-scanned."
                ),
            ),
            SettingsField(
                id="face_min_samples",
                title="Minimum photos per person",
                type=SettingsFieldType.NUMBER,
                default=3,
                description=(
                    "A face group must contain at least this many faces to become a person (DBSCAN "
                    "min_samples). Lower finds more (smaller) people but with more false groupings."
                ),
            ),
            # ── Location (GPS) ───────────────────────────────────────────────
            SettingsField(
                id="geotag",
                title="Use photo GPS for location tags and context",
                type=SettingsFieldType.CHECKBOX,
                default=True,
                description=(
                    "Reads GPS coordinates from photo EXIF, reverse-geocodes them to place and "
                    "landmark names, adds those as tags, and tells the vision model where the photo "
                    "was taken so descriptions can name the actual place."
                ),
            ),
            SettingsField(
                id="nominatim_url",
                title="Nominatim server",
                type=SettingsFieldType.URL,
                default="https://nominatim.openstreetmap.org",
                description=(
                    "Reverse-geocoding endpoint. The default is the public OpenStreetMap instance "
                    "(rate-limited to 1 request/s; results are cached forever, so steady-state "
                    "traffic is minimal). Point this at a self-hosted Nominatim to keep photo "
                    "coordinates entirely on your network."
                ),
            ),
            SettingsField(
                id="face_match_min_similarity",
                title="Face match strictness (0–1)",
                type=SettingsFieldType.TEXT,  # decimal — NUMBER fields step in integers
                default="0.5",
                description=(
                    "Cosine similarity required to consider two faces the same person, used both for "
                    "real-time matching of new uploads and for clustering (eps = 1 − this). "
                    "Raise toward 0.6–0.7 for purer groups, lower for more merging. 0.5 is a good start."
                ),
            ),
        ],
    )


def register(nc: NextcloudApp) -> None:
    nc.ui.settings.register_form(_form())


def unregister(nc: NextcloudApp) -> None:
    nc.ui.settings.unregister_form(FORM_ID)

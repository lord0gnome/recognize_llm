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
        description="Tag and describe images with a local llama.cpp vision model.",
        fields=[
            SettingsField(
                id="llama_url",
                title="Vision endpoint URL",
                type=SettingsFieldType.URL,
                default="http://192.168.0.143:11434/v1",
                description="OpenAI-compatible base URL (with or without a trailing /v1). "
                "Must be reachable from the exApp container.",
                placeholder="http://host:port/v1",
            ),
            SettingsField(
                id="llama_model",
                title="Model name",
                type=SettingsFieldType.TEXT,
                default="",
                description="Leave empty for a single-model llama.cpp server; set it if the endpoint "
                "serves multiple models.",
                placeholder="(optional)",
            ),
            SettingsField(
                id="api_key",
                title="API key",
                type=SettingsFieldType.PASSWORD,
                default="",
                description="Optional bearer token, if your endpoint requires one.",
                sensitive=True,
            ),
            SettingsField(
                id="prompt",
                title="Prompt",
                type=SettingsFieldType.TEXT,
                default=DEFAULT_PROMPT,
                description="Instruction sent with each image. Must ask for a JSON object with "
                '"description" and "tags".',
            ),
            SettingsField(
                id="max_tags",
                title="Max tags per image",
                type=SettingsFieldType.NUMBER,
                default=8,
            ),
            SettingsField(
                id="mimetypes",
                title="Image mimetypes to process",
                type=SettingsFieldType.TEXT,
                default="image/jpeg,image/png,image/webp,image/heic,image/heif,image/gif,image/tiff",
                description="Comma-separated allowlist.",
            ),
            SettingsField(
                id="concurrency",
                title="Worker concurrency",
                type=SettingsFieldType.NUMBER,
                default=1,
                description="Parallel captioning jobs. Keep low unless your endpoint handles "
                "concurrent requests well. Increasing it takes effect after re-enabling the app.",
            ),
            SettingsField(
                id="request_timeout",
                title="Request timeout (seconds)",
                type=SettingsFieldType.NUMBER,
                default=180,
            ),
            SettingsField(
                id="write_comment",
                title="Also write the description as a file comment",
                type=SettingsFieldType.CHECKBOX,
                default=True,
                description="Makes the description visible in the Files 'Comments' tab "
                "(it is always stored as a DAV property regardless).",
            ),
        ],
    )


def register(nc: NextcloudApp) -> None:
    nc.ui.settings.register_form(_form())


def unregister(nc: NextcloudApp) -> None:
    nc.ui.settings.unregister_form(FORM_ID)

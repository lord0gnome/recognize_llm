"""Admin settings page (declarative settings).

Every configurable key from :mod:`settings` (i.e. every environment variable the app reads for its
behaviour) has a field here, grouped into one card per topic on a dedicated
**Administration → Recognize LLM** page.

How the pieces fit:

* AppAPI persists each field's value into the exApp's ``appconfig_ex`` keyed by the field ``id``,
  so the field ids here are exactly the keys read back in :mod:`settings`. No save endpoint needed.
* Workers re-read settings for every job, so most changes apply to the next image. Concurrency and
  upload-detection changes are applied within a minute by :mod:`settings_sync`.
* ExApps cannot register an admin *section* (core only accepts PHP ``IIconSection`` classes), so
  the section is provided by the tiny companion PHP app ``recognize_llm_settings``
  (``nextcloud/recognize_llm_settings`` in this repo). It advertises itself through a capability;
  without it the forms fall back to the stock "Additional settings" section so they never vanish.
* A field's default is what the key resolves to with nothing stored (its env var, else the
  built-in default), so the page always shows the value actually in effect. Secrets are the
  exception: their env value is never put in the form schema, which is sent to the browser.
"""

from __future__ import annotations

from nc_py_api import NextcloudApp
from nc_py_api.ex_app import LogLvl, SettingsField, SettingsFieldType, SettingsForm

import settings as settings_mod

# Section provided by the companion app, and the stock section used when it is missing.
SECTION_ID = "recognize_llm"
FALLBACK_SECTION_ID = "additional"
COMPANION_CAPABILITY = "recognize_llm_settings"

# Form ids must NOT contain "<app_id>_": Nextcloud's settings page sends
# `form.id.replace(app + "_", "")` as the formId on save, AppAPI then finds no form with that id
# and silently drops the value (HTTP 200, nothing stored). So "recognize_llm_endpoint" could
# never be saved; "rllm_endpoint" round-trips unchanged.
FORM_PREFIX = "rllm_"
# Forms registered by earlier versions: the single "Additional settings" form, and the first
# dedicated-page forms whose ids were mangled on save.
_LEGACY_FORM_IDS = ("recognize_llm_settings",) + tuple(
    "recognize_llm_" + n
    for n in ("endpoint", "captioning", "files", "workers", "people", "location", "detection"))


def _d(key: str) -> str:
    return settings_mod.fallback(key)


def _n(key: str) -> int:
    try:
        return int(float(_d(key)))
    except ValueError:
        return 0


def _b(key: str) -> bool:
    return str(_d(key)).lower() in ("1", "yes", "true", "on")


def _env(key: str) -> str:
    return f" (env {settings_mod.env_var(key)})"


def _field(key: str, title: str, ftype: SettingsFieldType, description: str, **kw) -> SettingsField:
    """A field whose id is the settings key; the env var name is appended to the description."""
    if "default" not in kw:
        if key in settings_mod.SENSITIVE_KEYS:
            kw["default"] = ""
        elif ftype == SettingsFieldType.CHECKBOX:
            kw["default"] = _b(key)
        elif ftype == SettingsFieldType.NUMBER:
            kw["default"] = _n(key)
        else:
            kw["default"] = _d(key)
    if key in settings_mod.SENSITIVE_KEYS:
        kw["sensitive"] = True
    return SettingsField(id=key, title=title, type=ftype, description=description + _env(key), **kw)


def _forms(section_id: str) -> list[SettingsForm]:
    T = SettingsFieldType

    def form(name: str, priority: int, title: str, description: str, fields: list[SettingsField]):
        return SettingsForm(
            id=FORM_PREFIX + name, section_type="admin", section_id=section_id,
            priority=priority, title=title, description=description, fields=fields,
        )

    return [
        form("endpoint", 10, "Vision model endpoint",
             "The OpenAI-compatible server that captions images. Changes apply to the next job; "
             "a queue paused because the endpoint was unreachable resumes on its own once the new "
             "address answers (checked every few minutes).",
             [
                 _field("llama_url", "Endpoint URL", T.URL,
                        "OpenAI-compatible base URL. It must be reachable from the exApp container, "
                        "not just from your browser. The /v1/chat/completions path is appended as "
                        "needed (http://host:8080, http://host:11434/v1 and a full "
                        ".../chat/completions URL all work).",
                        placeholder="http://host:port/v1"),
                 _field("llama_model", "Model name", T.TEXT,
                        "Model id sent with each request. Required for multi-model servers "
                        "(Ollama, llama-swap, Strata…); leave empty to let a single-model server "
                        "pick its default.",
                        placeholder="(server default)"),
                 _field("api_key", "API key", T.PASSWORD,
                        "Bearer token sent with every request. Leave empty if the endpoint needs "
                        "no authentication. Stored encrypted."),
                 _field("max_tokens", "Max response tokens", T.NUMBER,
                        "Token budget for the model's JSON reply. Below ~1024 a detailed "
                        "description with many tags can be cut off mid-JSON and fail the job. "
                        "Minimum 64."),
                 _field("request_timeout", "Request timeout (seconds)", T.NUMBER,
                        "How long to wait for the model per image before failing the attempt. "
                        "Raise it for slow hardware or large images. Minimum 10."),
             ]),
        form("captioning", 20, "Captioning",
             "What the model is asked for and what is written back to Nextcloud.",
             [
                 _field("prompt", "Prompt", T.TEXT,
                        "Instruction sent with each image. It must ask for a JSON object with a "
                        "\"description\" string and a \"tags\" string array — the only two fields "
                        "the app reads. Clear the field to go back to the built-in prompt."),
                 _field("max_tags", "Max tags per image", T.NUMBER,
                        "Cap on tags taken from the model per image; approved tag-consolidation "
                        "canonicals and GPS place tags are added on top of this cap."),
                 _field("write_comment", "Write the description as a file comment", T.CHECKBOX,
                        "Adds the description as a visible comment in Files → Comments. It is "
                        "always stored as a DAV property regardless."),
             ]),
        form("files", 30, "Files to process",
             "Which uploads are captioned. Files outside these lists are skipped silently.",
             [
                 _field("mimetypes", "Image types", T.TEXT,
                        "Comma-separated MIME type allowlist for images."),
                 _field("video_mimetypes", "Video types", T.TEXT,
                        "Comma-separated MIME type allowlist for videos (captioned from a 3×3 grid "
                        "of sampled frames; never face-scanned)."),
                 _field("max_file_mb", "Max file size (MB)", T.NUMBER,
                        "Larger files are skipped: the whole file is buffered in RAM, and a "
                        "multi-GB video can run the container out of memory."),
             ]),
        form("workers", 40, "Queue workers",
             "How hard the vision endpoint is driven.",
             [
                 _field("concurrency", "Worker concurrency", T.NUMBER,
                        "Images captioned in parallel. Keep at 1 unless the endpoint handles "
                        "parallel requests well. Applied within a minute, no restart needed; a "
                        "worker removed while busy finishes its current image first."),
             ]),
        form("people", 50, "People (face grouping)",
             "Faces are detected and grouped fully locally (InsightFace/ArcFace); face data is "
             "never sent to the vision endpoint. Review and name people from the People menu.",
             [
                 _field("face_clustering", "Group faces into people", T.CHECKBOX,
                        "Detect faces in processed photos and tag them person:…"),
                 _field("face_min_samples", "Minimum photos per person", T.NUMBER,
                        "A face group needs at least this many faces to become a person (DBSCAN "
                        "min_samples). Lower finds more, smaller people with more false groupings. "
                        "Minimum 2."),
                 _field("face_match_min_similarity", "Face match strictness (0.1–0.95)", T.TEXT,
                        "Cosine similarity needed to treat two faces as the same person, for both "
                        "real-time matching and clustering (eps = 1 − this). Raise toward 0.6–0.7 "
                        "for purer groups, lower for more merging."),
             ]),
        form("location", 60, "Location (GPS)",
             "Photo GPS coordinates become place and landmark tags and give the model context.",
             [
                 _field("geotag", "Use photo GPS for location tags and context", T.CHECKBOX,
                        "Reverse-geocode EXIF GPS to place names, tag them, and tell the model "
                        "where the photo was taken."),
                 _field("nominatim_url", "Nominatim server", T.URL,
                        "Reverse-geocoding endpoint. The public OpenStreetMap instance is "
                        "rate-limited (1 request/s; results are cached forever). Point this at a "
                        "self-hosted Nominatim to keep coordinates on your network."),
                 _field("overpass_url", "Overpass server (landmarks)", T.TEXT,
                        "Endpoint for the nearest-landmark lookup. Enter \"off\" to disable "
                        "landmark search."),
             ]),
        form("detection", 70, "Upload detection",
             "New files are picked up two ways: Nextcloud core webhooks (instant) and a periodic "
             "backstop scan. Webhook changes are re-registered automatically within a minute.",
             [
                 _field("webhook_enabled", "Instant detection via webhooks", T.CHECKBOX,
                        "Needs the webhook_listeners app (occ app:enable webhook_listeners)."),
                 _field("webhook_exapp_url", "ExApp URL reachable from Nextcloud", T.URL,
                        "Base URL Nextcloud uses to reach this exApp, without /events/webhook — "
                        "for a manual_install daemon, the LAN host:port it is registered with. "
                        "Empty disables webhooks (the scan still runs).",
                        placeholder="http://host:23000"),
                 _field("webhook_secret", "Webhook secret", T.PASSWORD,
                        "Shared secret Nextcloud sends with each webhook. Generated automatically "
                        "when empty; changing it re-registers the webhooks. Stored encrypted."),
                 _field("poll_enabled", "Periodic backstop scan", T.CHECKBOX,
                        "Scan every user's recently modified files to catch anything a webhook "
                        "missed."),
                 _field("poll_interval", "Scan interval (seconds)", T.NUMBER,
                        "Time between backstop scans. Minimum 60."),
             ]),
    ]


def form_keys() -> set[str]:
    """Every settings key that has a field on the admin page (used by the coverage test)."""
    return {f.id for form in _forms(SECTION_ID) for f in form.fields}


def target_section(nc: NextcloudApp) -> str:
    """The companion app's section if it is installed and enabled, else the stock fallback."""
    try:
        nc.update_server_info()  # capabilities are cached per session; re-read them
        if COMPANION_CAPABILITY in (nc.capabilities or {}):
            return SECTION_ID
    except Exception:
        pass
    return FALLBACK_SECTION_ID


def normalize_sensitive(nc: NextcloudApp) -> None:
    """Make sure every secret's appconfig_ex row exists and is flagged sensitive.

    AppAPI's settings form encrypts a password field on save but stores it with whatever
    ``sensitive`` flag the row already has (0 for a new row), and only decrypts on our read when the
    flag is 1. So a secret first set with ``occ app_api:app:config:set`` (plaintext, unflagged) shows
    as "Failed to decrypt" on the page, and one first typed into the page reaches us as ciphertext.
    Re-storing the current plaintext with ``sensitive=True`` (AppAPI encrypts it once) fixes both
    directions. Idempotent.
    """
    stored = {r.key: r.value for r in nc.appconfig_ex.get_values(list(settings_mod.SENSITIVE_KEYS))}
    for key in settings_mod.SENSITIVE_KEYS:
        value = stored.get(key) or ""
        if settings_mod.looks_encrypted(value):
            nc.log(LogLvl.ERROR,
                   f"recognize_llm: setting '{key}' was stored encrypted without AppAPI's sensitive "
                   f"flag, so its value cannot be read — re-enter it under Administration → "
                   f"Recognize LLM.")
            # Flag the row so the re-entered value round-trips. The unreadable value is dropped.
            value = ""
        nc.appconfig_ex.set_value(key, value, sensitive=True)


_registered_section: str | None = None


def register(nc: NextcloudApp, section_id: str | None = None) -> str:
    """(Re)register all forms in *section_id* (auto-detected when None). Returns the section."""
    global _registered_section
    section_id = section_id or target_section(nc)
    normalize_sensitive(nc)
    for legacy in _LEGACY_FORM_IDS:
        try:
            nc.ui.settings.unregister_form(legacy)
        except Exception:
            pass
    for form in _forms(section_id):
        nc.ui.settings.register_form(form)
    if section_id != _registered_section:
        where = ("Administration → Recognize LLM" if section_id == SECTION_ID else
                 "Administration → Additional settings (install the recognize_llm_settings app "
                 "for a dedicated page)")
        try:
            nc.log(LogLvl.INFO, f"recognize_llm: settings page registered under {where}")
        except Exception:
            pass
    _registered_section = section_id
    return section_id


def ensure_section(nc: NextcloudApp) -> None:
    """Move the forms if the companion app was installed or removed since they were registered."""
    section_id = target_section(nc)
    if section_id != _registered_section:
        register(nc, section_id)


def unregister(nc: NextcloudApp) -> None:
    global _registered_section
    for form_id in [f.id for f in _forms(SECTION_ID)] + list(_LEGACY_FORM_IDS):
        try:
            nc.ui.settings.unregister_form(form_id)
        except Exception:
            pass
    _registered_section = None

"""Admin settings page: every configurable env var has a field, secrets stay secret and round-trip,
the page lands in the companion section, and page changes are applied live. No network."""

import os
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
os.environ.setdefault("APP_ID", "recognize_llm")
os.environ.setdefault("APP_PERSISTENT_STORAGE", tempfile.mkdtemp())

from nc_py_api.ex_app import SettingsFieldType  # noqa: E402

import file_events  # noqa: E402
import job_queue  # noqa: E402
import settings as settings_mod  # noqa: E402
import settings_sync  # noqa: E402
import settings_ui  # noqa: E402

CIPHERTEXT = "a1b2c3|d4e5f6|0718293a|3"


class FakeAppConfig:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.sensitive = {}

    def get_values(self, keys):
        return [SimpleNamespace(key=k, value=self.values[k]) for k in keys if k in self.values]

    def set_value(self, key, value, sensitive=None):
        self.values[key] = value
        if sensitive is not None:
            self.sensitive[key] = sensitive


class FakeNC:
    def __init__(self, values=None, capabilities=None):
        self.appconfig_ex = FakeAppConfig(values)
        self.capabilities = capabilities or {}
        self.logs = []
        self.ocs_calls = []
        self.forms = {}
        self.ui = SimpleNamespace(settings=SimpleNamespace(
            register_form=lambda f: self.forms.__setitem__(f.id, f),
            unregister_form=lambda fid: self.forms.pop(fid, None),
        ))

    def update_server_info(self):
        pass

    def log(self, level, msg):
        self.logs.append(msg)

    def ocs(self, method, path, **kw):
        self.ocs_calls.append((method, path, kw))


def _all_fields():
    return [f for form in settings_ui._forms(settings_ui.SECTION_ID) for f in form.fields]


# ── Coverage ─────────────────────────────────────────────────────────────────────────────────────

def test_every_configurable_key_has_exactly_one_field():
    ids = [f.id for f in _all_fields()]
    assert len(ids) == len(set(ids)), "duplicate field ids"
    assert set(ids) == set(settings_mod.keys())


def test_descriptions_name_the_env_var():
    for f in _all_fields():
        assert settings_mod.env_var(f.id) in f.description, f.id


def test_forms_are_admin_and_ordered():
    forms = settings_ui._forms("x")
    assert all(f.section_type == "admin" and f.section_id == "x" for f in forms)
    assert [f.priority for f in forms] == sorted(f.priority for f in forms)
    assert len({f.id for f in forms}) == len(forms)


def test_form_ids_survive_the_settings_page_save_mangling():
    # Nextcloud's frontend posts formId = form.id.replace(app + "_", ""); AppAPI then looks the
    # form up by that exact id and silently drops the save when it is not found.
    app = os.environ["APP_ID"]
    for f in settings_ui._forms(settings_ui.SECTION_ID):
        assert f.id.replace(app + "_", "", 1) == f.id, f.id


def test_register_drops_forms_with_unsaveable_ids():
    nc = FakeNC(capabilities={"recognize_llm_settings": {}})
    nc.forms["recognize_llm_endpoint"] = object()
    settings_ui.register(nc)
    assert not any(fid.startswith("recognize_llm_") for fid in nc.forms)


# ── Defaults show the value in effect, never a secret ───────────────────────────────────────────

def test_defaults_follow_env_vars(monkeypatch):
    monkeypatch.setenv("MAX_TAGS", "12")
    monkeypatch.setenv("LLAMA_URL", "http://strata:5002/v1")
    monkeypatch.setenv("WRITE_COMMENT", "no")
    fields = {f.id: f for f in _all_fields()}
    assert fields["max_tags"].default == 12
    assert fields["llama_url"].default == "http://strata:5002/v1"
    assert fields["write_comment"].default is False


def test_secret_env_values_never_reach_the_form_schema(monkeypatch):
    monkeypatch.setenv("LLAMA_API_KEY", "sk-super-secret")
    monkeypatch.setenv("WEBHOOK_SECRET", "hook-secret")
    fields = {f.id: f for f in _all_fields()}
    for key in settings_mod.SENSITIVE_KEYS:
        assert fields[key].default == ""
        assert fields[key].sensitive is True
        assert fields[key].type == SettingsFieldType.PASSWORD
    for f in _all_fields():
        assert "sk-super-secret" not in str(f.default) and "hook-secret" not in str(f.default)


def test_overpass_off_disables_landmarks():
    nc = FakeNC({"overpass_url": "off"})
    assert settings_mod.load(nc).overpass_url == ""
    nc = FakeNC({"overpass_url": "https://example.org/api"})
    assert settings_mod.load(nc).overpass_url == "https://example.org/api"


# ── Secrets round-trip through the page ─────────────────────────────────────────────────────────

def test_looks_encrypted():
    assert settings_mod.looks_encrypted(CIPHERTEXT)
    assert not settings_mod.looks_encrypted("")
    assert not settings_mod.looks_encrypted("sk-strata-abc|def")
    assert not settings_mod.looks_encrypted("plain-api-key")


def test_normalize_reflags_plaintext_secrets():
    nc = FakeNC({"api_key": "plain-key"})
    settings_ui.normalize_sensitive(nc)
    assert nc.appconfig_ex.values["api_key"] == "plain-key"
    assert nc.appconfig_ex.sensitive == {"api_key": True, "webhook_secret": True}
    assert nc.logs == []


def test_normalize_drops_unreadable_ciphertext_and_says_so():
    nc = FakeNC({"api_key": CIPHERTEXT})
    settings_ui.normalize_sensitive(nc)
    assert nc.appconfig_ex.values["api_key"] == ""
    assert nc.appconfig_ex.sensitive["api_key"] is True
    assert any("re-enter" in m for m in nc.logs)


# ── Section selection ────────────────────────────────────────────────────────────────────────────

def test_register_uses_companion_section_and_drops_legacy_form():
    nc = FakeNC(capabilities={"recognize_llm_settings": {"admin_section": "recognize_llm"}})
    nc.forms["recognize_llm_settings"] = object()  # the old single form
    assert settings_ui.register(nc) == settings_ui.SECTION_ID
    assert "recognize_llm_settings" not in nc.forms
    assert {f.section_id for f in nc.forms.values()} == {settings_ui.SECTION_ID}


def test_register_falls_back_without_companion():
    nc = FakeNC()
    assert settings_ui.register(nc) == settings_ui.FALLBACK_SECTION_ID
    assert {f.section_id for f in nc.forms.values()} == {settings_ui.FALLBACK_SECTION_ID}


def test_ensure_section_moves_forms_when_companion_appears():
    nc = FakeNC()
    settings_ui.register(nc)
    nc.capabilities = {"recognize_llm_settings": {}}
    settings_ui.ensure_section(nc)
    assert {f.section_id for f in nc.forms.values()} == {settings_ui.SECTION_ID}


# ── Live application: webhooks ───────────────────────────────────────────────────────────────────

def _cfg(**kw):
    base = dict(webhook_enabled=True, webhook_exapp_url="http://apoc:23000", webhook_secret="s1")
    base.update(kw)
    return SimpleNamespace(**base)


def _creates(nc):
    return [c for c in nc.ocs_calls if c[0] == "POST"]


def test_sync_webhooks_registers_once_then_only_on_change():
    nc = FakeNC()
    assert file_events.sync_webhooks(nc, _cfg()) is True
    assert len(_creates(nc)) == 2
    assert file_events.sync_webhooks(nc, _cfg()) is False
    assert len(_creates(nc)) == 2
    assert file_events.sync_webhooks(nc, _cfg(webhook_exapp_url="http://new:23000")) is True
    uris = {c[2]["json"]["uri"] for c in _creates(nc)[2:]}
    assert uris == {"http://new:23000/events/webhook"}


def test_secret_change_reregisters_and_receiver_accepts_it():
    nc = FakeNC()
    file_events.sync_webhooks(nc, _cfg())
    file_events.sync_webhooks(nc, _cfg(webhook_secret="s2"))
    assert _creates(nc)[-1][2]["json"]["authData"] == {file_events.WEBHOOK_SECRET_HEADER: "s2"}
    assert file_events.cached_secret() == "s2"


def test_disabling_webhooks_removes_them():
    nc = FakeNC()
    file_events.sync_webhooks(nc, _cfg())
    nc.ocs_calls.clear()
    file_events.sync_webhooks(nc, _cfg(webhook_enabled=False))
    assert [c[0] for c in nc.ocs_calls] == ["DELETE"]
    assert file_events.sync_webhooks(nc, _cfg(webhook_enabled=False)) is False


def test_generated_secret_does_not_cause_reregistration_loop():
    nc = FakeNC()
    file_events.sync_webhooks(nc, _cfg(webhook_secret=""))
    generated = nc.appconfig_ex.values["webhook_secret"]
    assert generated and nc.appconfig_ex.sensitive["webhook_secret"] is True
    assert file_events.sync_webhooks(nc, _cfg(webhook_secret=generated)) is False


# ── Live application: worker pool size ───────────────────────────────────────────────────────────

def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_concurrency_grows_and_shrinks_live(monkeypatch):
    monkeypatch.setattr(job_queue, "NextcloudApp", lambda: None)
    monkeypatch.setattr(job_queue, "_claim", lambda: None)
    real_sleep = time.sleep
    monkeypatch.setattr(job_queue.time, "sleep", lambda s: real_sleep(0.01))
    w = job_queue.Workers()
    w._supervisor = threading.Thread(target=lambda: None)  # keep the 60s supervisor out of it
    w._supervisor.start()
    try:
        w.start(1)
        assert _wait(lambda: w.alive() == 1)
        assert w.set_concurrency(3) is True
        assert _wait(lambda: sum(t.is_alive() for t in w._threads.values()) == 3)
        assert w.set_concurrency(3) is False
        w.set_concurrency(1)
        assert _wait(lambda: sum(t.is_alive() for t in w._threads.values()) == 1)
        assert w.alive() == 1
    finally:
        w.stop()


def test_settings_sync_applies_concurrency_and_webhooks():
    nc = FakeNC()
    workers = mock.Mock()
    workers.set_concurrency.return_value = True
    with mock.patch.object(file_events, "sync_webhooks") as sync:
        settings_sync.SettingsSync(workers).apply(nc, SimpleNamespace(concurrency=2))
    workers.set_concurrency.assert_called_once_with(2)
    sync.assert_called_once()

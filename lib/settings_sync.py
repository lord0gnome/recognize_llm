"""Apply admin-page changes that a worker can't pick up on its own.

Workers reload settings for every job, so most keys take effect on the next image. Three things are
set up once and would otherwise need an app re-enable or a container restart:

* the worker pool size (``concurrency``),
* the core webhooks (``webhook_enabled`` / ``webhook_exapp_url`` / ``webhook_secret``),
* which admin section the settings forms live in (the companion app may be installed later).

This thread re-checks them every minute. It also (re)registers the forms once at startup, so a
new image moves the page without anyone having to disable/enable the exApp.
"""

from __future__ import annotations

import threading
import time

import file_events
import settings as settings_mod
import settings_ui
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import LogLvl

INTERVAL = 60.0
SECTION_RECHECK = 15 * 60.0  # the companion-app check reads capabilities; no need to do it often
STARTUP_DELAY = 20.0


class SettingsSync:
    def __init__(self, workers):
        self._workers = workers
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="recognize-llm-settings-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        if self._stop.wait(STARTUP_DELAY):
            return
        nc: NextcloudApp | None = None
        forms_ok = False
        last_section_check = 0.0
        while not self._stop.is_set():
            try:
                if nc is None:
                    nc = NextcloudApp()
                if not forms_ok:
                    settings_ui.register(nc)
                    forms_ok = True
                    last_section_check = time.time()
                elif time.time() - last_section_check > SECTION_RECHECK:
                    settings_ui.ensure_section(nc)
                    last_section_check = time.time()
                self.apply(nc, settings_mod.load(nc))
            except Exception:
                # NC unreachable, or the app is disabled (AppAPI then refuses our calls). Rebuild
                # the session next round; nothing here is urgent enough to log every minute.
                nc = None
            if self._stop.wait(INTERVAL):
                return

    def apply(self, nc: NextcloudApp, cfg) -> None:
        if self._workers.set_concurrency(cfg.concurrency):
            _log(nc, f"worker concurrency set to {cfg.concurrency}")
        file_events.sync_webhooks(nc, cfg)


def _log(nc, msg: str) -> None:
    try:
        nc.log(LogLvl.INFO, f"recognize_llm: {msg}")
    except Exception:
        pass

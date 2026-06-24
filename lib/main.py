"""recognize_llm exApp entrypoint: lifecycle, routes, and the on-demand file action."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Annotated

import job_queue
import routes_backfill
import routes_dashboard
import routes_events
import settings as settings_mod
import settings_ui
import task_provider
from fastapi import Depends, FastAPI, responses
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import AppAPIAuthMiddleware, LogLvl, nc_app, run_app, set_handlers
from nc_py_api.files import ActionFileInfoEx

EVENTS_LISTENER_PATH = "/events/node"
_AE_EVENTS_URL = "/ocs/v1.php/apps/app_api/api/v1/events_listener"

workers = job_queue.Workers()
provider_loop = task_provider.ProviderLoop()


@asynccontextmanager
async def lifespan(app: FastAPI):
    set_handlers(app, enabled_handler)
    job_queue.init_db()
    # Start the local worker pool + provider loop so a restart resumes any unfinished queue.
    workers.start(int(os.environ.get("CONCURRENCY", "1") or 1))
    provider_loop.start()
    yield
    workers.stop()
    provider_loop.stop()


APP = FastAPI(lifespan=lifespan)
APP.add_middleware(AppAPIAuthMiddleware)
APP.include_router(routes_events.router)
APP.include_router(routes_backfill.router)
APP.include_router(routes_dashboard.router)


@APP.post("/describe_now")
async def describe_now(
    files: ActionFileInfoEx,
    nc: Annotated[NextcloudApp, Depends(nc_app)],
) -> responses.Response:
    """Files dropdown action: (re)describe the selected images now."""
    for f in files.files:
        if f.fileType.lower() == "file" and f.mime.lower().startswith("image/"):
            job_queue.enqueue(f.userId, f.fileId, source="manual", force=True)
    return responses.Response()


def _register_events_listener(nc: NextcloudApp) -> None:
    nc.ocs(
        "POST",
        _AE_EVENTS_URL,
        json={
            "eventType": "node_event",
            "actionHandler": EVENTS_LISTENER_PATH,
            "eventSubtypes": ["NodeCreatedEvent", "NodeWrittenEvent"],
        },
    )


def _unregister_events_listener(nc: NextcloudApp) -> None:
    nc.ocs("DELETE", _AE_EVENTS_URL, params={"eventType": "node_event"})


def enabled_handler(enabled: bool, nc: NextcloudApp) -> str:
    try:
        if enabled:
            cfg = settings_mod.load(nc)
            settings_ui.register(nc)
            task_provider.register(nc)
            _register_events_listener(nc)
            nc.ui.files_dropdown_menu.register_ex(
                "recognize_llm_describe", "Describe with AI", "/describe_now",
                mime="image", icon="img/icon.svg",
            )
            nc.ui.top_menu.register("dashboard", "AI Queue", icon="img/icon.svg", admin_required=True)
            nc.ocs("POST", "/ocs/v1.php/apps/app_api/api/v1/ui/script", json={
                "type": "top_menu", "name": "dashboard",
                "path": "js/dashboard-loader", "afterAppId": "",
            })
            nc.occ_commands.register(
                "recognize_llm:backfill",
                "/occ/backfill",
                options=[
                    {"name": "users", "shortcut": "u", "mode": "optional", "description": "Comma-separated user IDs to scan (default: all users)", "default": ""},
                    {"name": "path",  "shortcut": "p", "mode": "optional", "description": "Restrict scan to this folder path (default: all files)",  "default": ""},
                ],
                description="Enqueue existing images for AI tagging and description.",
            )
            workers.start(cfg.concurrency)
            provider_loop.start()
            nc.log(LogLvl.INFO, "recognize_llm enabled")
        else:
            settings_ui.unregister(nc)
            task_provider.unregister(nc)
            _unregister_events_listener(nc)
            nc.ui.files_dropdown_menu.unregister("recognize_llm_describe")
            nc.ui.top_menu.unregister("dashboard")
            nc.ocs("DELETE", "/ocs/v1.php/apps/app_api/api/v1/ui/script", params={
                "type": "top_menu", "name": "dashboard", "path": "js/dashboard-loader",
            })
            nc.occ_commands.unregister("recognize_llm:backfill")
            nc.log(LogLvl.INFO, "recognize_llm disabled")
    except Exception as e:
        return str(e)
    return ""


if __name__ == "__main__":
    run_app("main:APP", log_level="info")

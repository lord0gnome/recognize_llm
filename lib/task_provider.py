"""TaskProcessing provider: exposes the vision engine to other Nextcloud AI features.

Registers a custom task type ``recognize_llm:image2text`` (image in -> description + tags out) and
runs a background loop that pulls tasks from Nextcloud, captions them, and reports the result.
"""

from __future__ import annotations

import threading
import time

import processor
import settings as settings_mod
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import LogLvl
from nc_py_api.ex_app.providers.task_processing import ShapeType, TaskProcessingProvider

PROVIDER_ID = "recognize_llm"
TASK_TYPE_ID = "recognize_llm:image2text"

# Built as a plain dict (not nc_py_api's TaskType dataclass): app_api 34's
# TaskProcessingService::getAnonymousTaskType() reads each shape's enum under the key "shape_type"
# (confirmed from the running server's exception log: "Undefined array key \"shape_type\"" when a
# "type" key was used instead — that mismatch silently drops the custom task type from
# Manager::getAvailableTaskTypes(), which also empties tasktypes for every OTHER provider since the
# whole request throws).
# register() passes a dict straight through (RootModel(dict).model_dump()), so the keys land as-is.
_TASK_TYPE = {
    "id": TASK_TYPE_ID,
    "name": "Describe image (local vision)",
    "description": "Generate a description and tags for an image using a local llama.cpp vision model",
    "input_shape": [
        {"name": "image", "description": "The image to describe", "shape_type": int(ShapeType.IMAGE)},
    ],
    "output_shape": [
        {"name": "description", "description": "Generated description", "shape_type": int(ShapeType.TEXT)},
        {"name": "tags", "description": "Comma-separated tags", "shape_type": int(ShapeType.TEXT)},
    ],
}

_PROVIDER = TaskProcessingProvider(
    id=PROVIDER_ID,
    name="Recognize LLM (local vision)",
    task_type=TASK_TYPE_ID,
    expected_runtime=30,
)


def register(nc: NextcloudApp) -> None:
    nc.providers.task_processing.register(_PROVIDER, _TASK_TYPE)


def unregister(nc: NextcloudApp) -> None:
    nc.providers.task_processing.unregister(PROVIDER_ID)


def _download_input_file(nc: NextcloudApp, task_id: int, file_id: int) -> bytes:
    """Fetch a TaskProcessing input file's raw bytes.

    NOTE: verify against the running server during M4 — the tasks_provider file-read route shape can
    vary by Nextcloud version. Adjust the path/method here if the live API differs.
    """
    resp = nc._session.adapter.get(
        f"/ocs/v2.php/taskprocessing/tasks_provider/{task_id}/file/{file_id}",
        headers={"OCS-APIRequest": "true"},
    )
    resp.raise_for_status()
    return resp.content


def _handle_task(nc: NextcloudApp, task: dict) -> None:
    task_id = task["id"]
    try:
        cfg = settings_mod.load(nc)
        image_ref = task["input"]["image"]
        image = _download_input_file(nc, task_id, int(image_ref))
        caption = processor.caption_bytes(image, "image/jpeg", cfg)
        nc.providers.task_processing.report_result(
            task_id,
            output={"description": caption.description, "tags": ", ".join(caption.tags)},
        )
    except Exception as e:
        nc.log(LogLvl.ERROR, f"recognize_llm: task {task_id} failed: {e}")
        nc.providers.task_processing.report_result(task_id, error_message=str(e))


class ProviderLoop:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="recognize-llm-provider", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        nc = NextcloudApp()
        while not self._stop.is_set():
            try:
                result = nc.providers.task_processing.next_task([PROVIDER_ID], [TASK_TYPE_ID])
                task = result.get("task") if result else None
                if not task:
                    time.sleep(5.0)
                    continue
                _handle_task(nc, task)
            except Exception as e:
                nc.log(LogLvl.ERROR, f"recognize_llm: provider loop error: {e}")
                time.sleep(5.0)

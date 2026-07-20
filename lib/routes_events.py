"""HTTP receivers that turn Nextcloud file events into queue jobs.

Two entry points feed the same queue (see lib/file_events.py for the why):

* ``/events/webhook`` — Nextcloud core ``webhook_listeners`` POSTs upload events here. This is the
  primary, update-proof path. It is exempt from ``AppAPIAuthMiddleware`` (the request comes straight
  from NC core, not through the AppAPI proxy) and is instead authenticated by a shared secret header.
* ``/events/node`` — legacy receiver for the hand-patched AppAPI ``events_listener``. Kept working
  for any deployment that still carries that core patch; harmless where it doesn't.
"""

from __future__ import annotations

import hmac

import file_events
from fastapi import APIRouter, Request, responses

router = APIRouter()


@router.post(file_events.WEBHOOK_PATH)
async def on_webhook(request: Request) -> responses.Response:
    # Verify the shared secret before doing anything else — this route bypasses AppAPI auth.
    secret = file_events.cached_secret()
    sent = request.headers.get(file_events.WEBHOOK_SECRET_HEADER, "")
    if not secret or not hmac.compare_digest(sent, secret):
        return responses.Response(status_code=401)
    try:
        file_events.enqueue_from_webhook(await request.json())
    except Exception:
        pass  # never make NC's webhook dispatcher retry on our parsing hiccup
    return responses.Response()


@router.post("/events/node")
async def on_node_event(request: Request) -> responses.Response:
    # Parse raw JSON — tolerates schema drift between NC/AppAPI versions (e.g. favorite: bool vs str).
    try:
        body = await request.json()
        file_events.enqueue_target(body.get("event_data", {}).get("target", {}), source="event")
    except Exception:
        pass
    return responses.Response()

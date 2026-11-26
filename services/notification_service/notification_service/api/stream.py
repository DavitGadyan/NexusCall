"""`GET /api/v1/notifications/stream`: one Server-Sent Events stream per browser tab.

Relays two Redis channels of the caller's tenant, filtered for this viewer:
* `ui:{tenant}` (from `nexus_core.realtime.publish_ui_event`) -> `{type:"ui", event, data}` only when the
  viewer holds the event's `permission`;
* `notify:{tenant}` -> `{type:"notification", id}` only for notifications addressed to the viewer.

Heartbeats are SSE comments. Membership and permissions are re-resolved periodically, so a revoked
member is disconnected; the stream also ends after `sse_max_seconds` and the client reconnects with a
fresh token. Concurrent streams per user are capped.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from nexus_core.authz import AuthDep
from nexus_core.errors import ServiceUnavailable, TooManyRequests
from nexus_core.ratelimit import acquire_slot, release_slot
from nexus_core.realtime import ui_channel
from notification_service.api.notifications import load_preferences, viewer_of
from notification_service.domain import Viewer, notify_channel, relay

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])
log = logging.getLogger(__name__)


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


async def event_stream(request: Request, viewer: Viewer, principal: Any, pubsub: Any) -> AsyncIterator[str]:
    settings = request.app.state.settings
    resolver = request.app.state.authz_resolver
    started = last_beat = last_check = time.monotonic()
    yield "retry: 5000\n\n"
    try:
        while True:
            now = time.monotonic()
            if now - started >= settings.sse_max_seconds:
                break
            if await request.is_disconnected():
                break
            if now - last_check >= settings.sse_authz_recheck_seconds:
                last_check = now
                access = await resolver.resolve_tenant(principal, viewer.tenant_id)
                if not access.is_member or access.tenant_status != "active":
                    break
                viewer.permissions = access.permissions
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=min(1.0, settings.sse_heartbeat_seconds)
            )
            if message and message.get("type") == "message":
                channel = message.get("channel")
                channel = channel.decode() if isinstance(channel, bytes | bytearray) else str(channel)
                out = relay(channel, message.get("data"), viewer)
                if out is not None:
                    yield _sse(out)
            if time.monotonic() - last_beat >= settings.sse_heartbeat_seconds:
                last_beat = time.monotonic()
                yield ": heartbeat\n\n"
    finally:
        # Shielded: this runs while the task is being cancelled when the client disconnects.
        with anyio.CancelScope(shield=True), anyio.move_on_after(5):
            try:
                await pubsub.unsubscribe()
                await pubsub.aclose()
            except Exception:  # noqa: BLE001 - connection may already be gone
                log.debug("sse_pubsub_close_failed")


class _LeasedStreamingResponse(StreamingResponse):
    """Runs `on_close` however the response ends. A client that disconnects before the body starts makes
    Starlette cancel the response before the body generator's first step; that generator's `finally`
    then never runs, so releasing there alone leaks the stream slot until its lease expires."""

    def __init__(self, content: AsyncIterator[str], *, on_close: Callable[[], Awaitable[None]], **kw: Any) -> None:
        super().__init__(content, **kw)
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True), anyio.move_on_after(5):
                await self._on_close()


@router.get("/stream")
async def stream(request: Request, ctx: AuthDep) -> StreamingResponse:
    tenant_id, user_id = viewer_of(ctx)
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise ServiceUnavailable("Live updates are unavailable")
    settings = request.app.state.settings
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as s:
        prefs = await load_preferences(s, user_id)
    viewer = Viewer(tenant_id, user_id, ctx.permissions, frozenset(prefs["muted_kinds"]))
    slot_key, member = f"sse:{tenant_id}:{user_id}", str(uuid.uuid4())
    if not await acquire_slot(
        redis,
        slot_key,
        member,
        max_slots=settings.sse_max_streams_per_user,
        ttl_seconds=int(settings.sse_max_seconds) + 60,
    ):
        raise TooManyRequests("Too many open live-update streams")
    pubsub = redis.pubsub()

    async def close() -> None:
        try:
            await pubsub.aclose()
        except Exception:  # noqa: BLE001 - connection may already be gone
            log.debug("sse_pubsub_close_failed")
        try:
            await release_slot(redis, slot_key, member)
        except Exception:  # noqa: BLE001 - the slot lease expires on its own
            log.debug("sse_slot_release_failed")

    try:
        await pubsub.subscribe(ui_channel(tenant_id), notify_channel(tenant_id))
    except BaseException:
        with anyio.CancelScope(shield=True):
            await close()
        raise

    return _LeasedStreamingResponse(
        event_stream(request, viewer, ctx.principal, pubsub),
        on_close=close,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )

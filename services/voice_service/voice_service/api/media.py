"""`WS /media/twilio`: Twilio Media Streams (https://www.twilio.com/docs/voice/media-streams/websocket-messages).

No stream is ever processed unauthenticated: until a `start` message carries a valid, unexpired, single-use
stream token bound to the same CallSid, every message (including media) is discarded; a missing or bad token
closes the socket with 1008 (policy violation) before any provider, model or database work happens.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from voice_service.deps import AppDeps
from voice_service.pipeline import Conversation, run_with_timeout
from voice_service.session import SessionRejected, TwilioSink, VoiceCallSession
from voice_service.stream_auth import StreamAuthError, claim_once, verify_stream_token

router = APIRouter(tags=["media"])
log = logging.getLogger(__name__)
POLICY_VIOLATION = 1008
MAX_MESSAGE_BYTES = 64 * 1024


async def _await_start(ws: WebSocket) -> dict[str, Any]:
    while True:
        raw = await ws.receive_text()
        if len(raw) > MAX_MESSAGE_BYTES:
            raise StreamAuthError("message_too_large")
        data = json.loads(raw)
        if data.get("event") == "start":
            return dict(data)
        # `connected` and anything sent before `start` (including media) is ignored.


async def _reader(ws: WebSocket, conversation: Conversation) -> None:
    try:
        while True:
            raw = await ws.receive_text()
            if len(raw) > MAX_MESSAGE_BYTES:
                continue
            data = json.loads(raw)
            event = data.get("event")
            if event == "media":
                media = data.get("media") or {}
                if media.get("track", "inbound") != "inbound":
                    continue
                try:
                    audio = base64.b64decode(media.get("payload", ""), validate=True)
                except (binascii.Error, ValueError):
                    continue
                await conversation.on_media(audio)
            elif event == "mark":
                conversation.on_mark(str((data.get("mark") or {}).get("name", "")))
            elif event == "stop":
                conversation.on_stop()
                return
    except (WebSocketDisconnect, RuntimeError, ValueError):
        conversation.on_stop()


@router.websocket("/media/twilio")
async def media_stream(ws: WebSocket) -> None:
    deps = AppDeps(ws.app)
    await ws.accept()
    try:
        start = await asyncio.wait_for(_await_start(ws), timeout=deps.settings.start_timeout_seconds)
        info = start.get("start") or {}
        claims = verify_stream_token(
            deps.settings.stream_secret(),
            (info.get("customParameters") or {}).get("token"),
            call_sid=info.get("callSid"),
        )
        await claim_once(deps.redis, claims)
    except (TimeoutError, StreamAuthError, ValueError, WebSocketDisconnect) as exc:
        log.warning("media_stream_rejected", extra={"reason": str(exc)[:40] or type(exc).__name__})
        with contextlib.suppress(Exception):
            await ws.close(code=POLICY_VIOLATION)
        return
    stream_sid = str(start.get("streamSid") or info.get("streamSid") or "")
    session = VoiceCallSession(deps, claims, stream_sid, gateway=ws.app.state.gateway, tts=ws.app.state.tts)
    conversation: Conversation | None = None
    reason = "provider_failure"
    try:
        conversation = await session.prepare(TwilioSink(ws, stream_sid))
    except SessionRejected as exc:
        log.warning("media_session_rejected", extra={"reason": str(exc)})
        with contextlib.suppress(Exception):
            await ws.close(code=POLICY_VIOLATION)
        return
    except Exception:
        log.exception("media_session_setup_failed", extra={"call_id": str(claims.call_id)})
    if conversation is not None:
        reader = asyncio.create_task(_reader(ws, conversation))
        try:
            reason = await run_with_timeout(conversation)
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
    await session.finalize(conversation, reason)
    with contextlib.suppress(Exception):
        await ws.close()

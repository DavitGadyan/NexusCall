"""Request-ID / correlation propagation and request body size limits."""

from __future__ import annotations

import re
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from nexus_core.context import correlation_id_var, request_id_var, tenant_id_var
from nexus_core.errors import problem

_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def _header(scope: Scope, name: bytes) -> str | None:
    for k, v in scope.get("headers", []):
        if k == name:
            return str(v.decode("latin-1"))
    return None


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        rid = _header(scope, b"x-request-id")
        rid = rid if rid and _SAFE_ID.match(rid) else str(uuid.uuid4())
        cid = _header(scope, b"x-correlation-id")
        cid = cid if cid and _SAFE_ID.match(cid) else rid
        t1, t2, t3 = request_id_var.set(rid), correlation_id_var.set(cid), tenant_id_var.set(None)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", rid.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_var.reset(t1)
            correlation_id_var.reset(t2)
            tenant_id_var.reset(t3)


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = _header(scope, b"content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            await problem(413, "payload_too_large", "Payload too large", "Request body exceeds limit")(
                scope, receive, send
            )
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _TooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _TooLarge:
            await problem(413, "payload_too_large", "Payload too large", "Request body exceeds limit")(
                scope, receive, send
            )


class _TooLarge(Exception):
    pass

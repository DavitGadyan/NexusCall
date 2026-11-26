"""The SSE slot is released even when the client is gone before the body generator starts.

Regression: fast page changes left every stream slot taken (HTTP 429 on the live-update stream for
16 minutes), because the release lived only in the body generator's `finally`, which never runs when
Starlette cancels the response before the generator's first step.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anyio
import anyio.lowlevel
import pytest

from notification_service.api.stream import _LeasedStreamingResponse

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


SCOPE: dict[str, Any] = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}}


async def _already_disconnected() -> dict[str, Any]:
    return {"type": "http.disconnect"}


async def test_on_close_runs_when_the_client_left_before_the_body_started() -> None:
    started, closed = [], []

    async def body() -> AsyncIterator[str]:
        started.append(True)
        yield "retry: 5000\n\n"

    async def slow_send(message: dict[str, Any]) -> None:
        await anyio.sleep(10)  # the disconnect wins the race before the first body step

    async def on_close() -> None:
        await anyio.lowlevel.checkpoint()  # an await in the cleanup must survive the cancellation
        closed.append(True)

    resp = _LeasedStreamingResponse(body(), on_close=on_close, media_type="text/event-stream")
    with anyio.fail_after(5):
        await resp(SCOPE, _already_disconnected, slow_send)
    assert started == []  # the scenario under test: the generator never ran
    assert closed == [True]


async def test_on_close_runs_after_a_normal_end() -> None:
    closed, sent = [], []

    async def body() -> AsyncIterator[str]:
        yield "data: 1\n\n"

    async def send(message: dict[str, Any]) -> None:
        sent.append(message["type"])

    async def never_disconnects() -> dict[str, Any]:
        await anyio.sleep(10)
        return {"type": "http.disconnect"}

    async def on_close() -> None:
        closed.append(True)

    resp = _LeasedStreamingResponse(body(), on_close=on_close, media_type="text/event-stream")
    with anyio.fail_after(5):
        await resp(SCOPE, never_disconnects, send)
    assert "http.response.start" in sent
    assert closed == [True]

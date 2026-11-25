"""Fixtures: one real PostgreSQL per session (restricted roles, forced RLS), locally signed JWTs, static authz,
and a helper that serves an ASGI app on 127.0.0.1 (WebSocket tests need a real loopback socket)."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from nexus_core.testing import TokenFactory, provision_and_migrate, start_postgres

SERVICE_DIR = Path(__file__).resolve().parents[1]
FAKE_SECRET = "voice-test-fake-secret"


@pytest.fixture(scope="session")
def pg():
    try:
        container = start_postgres()
    except Exception as exc:  # noqa: BLE001 - skip when Docker is unavailable
        pytest.skip(f"Docker unavailable: {exc}")
    yield container
    container.stop()


@pytest.fixture(scope="session")
def sdb(pg):
    return provision_and_migrate(pg, "voice", SERVICE_DIR)


@pytest.fixture(scope="session")
def tokens():
    return TokenFactory()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def serve(app: Any) -> Iterator[str]:
    """Run `app` with uvicorn on a loopback port in a background thread; yields its base URL."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("test server did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def server():
    return serve


@pytest.fixture
def fake_providers_app(monkeypatch):
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("FAKE_PROVIDER_SECRET", FAKE_SECRET)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    from fake_providers.main import create_app

    return create_app()


def run(coro: Any) -> Any:
    return asyncio.run(coro)

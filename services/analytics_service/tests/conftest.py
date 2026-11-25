"""Integration fixtures: one PostgreSQL container per session (restricted app role, forced RLS, the
read-only platform role), locally signed JWTs, static authz, an in-memory object store and a recording
Celery stand-in (tasks are executed by calling the coroutine directly)."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from analytics_service.settings import Settings
from nexus_core.db import Database
from nexus_core.storage import InMemoryObjectStore
from nexus_core.testing import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    StaticAuthzResolver,
    TokenFactory,
    provision_and_migrate,
    start_postgres,
)

SERVICE_DIR = Path(__file__).resolve().parents[1]


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
    return provision_and_migrate(pg, "analytics", SERVICE_DIR)


@pytest.fixture(scope="session")
def tokens():
    return TokenFactory()


class RecordingCelery:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail = False

    def send_task(self, name: str, kwargs: dict[str, Any], queue: str, countdown: float | None = None) -> None:
        if self.fail:
            raise ConnectionError("broker down")
        self.sent.append({"name": name, "kwargs": kwargs, "queue": queue})


@pytest.fixture
def run_db(sdb):
    """Run `fn(database)` on a fresh pool (asyncpg pools are bound to one event loop)."""

    def _run(fn: Any, url: str | None = None) -> Any:
        async def go() -> Any:
            database = Database(url or sdb.app_url, pool_size=3, max_overflow=0)
            try:
                return await fn(database)
            finally:
                await database.dispose()

        return asyncio.run(go())

    return _run


class Harness:
    def __init__(self, client, app, authz, tokens, store, celery):
        self.client, self.app, self.authz, self.tokens, self.store, self.celery = (
            client,
            app,
            authz,
            tokens,
            store,
            celery,
        )

    def member(self, tenant_id: uuid.UUID, perms: set[str], *, mfa: bool = False) -> tuple[str, str]:
        uid = str(uuid.uuid4())
        self.authz.grant(uid, tenant_id, perms)
        return uid, self.tokens.user(uid, f"u-{uid[:6]}@x.test", mfa=mfa)

    def platform_user(self, perms: set[str]) -> tuple[str, str]:
        uid = str(uuid.uuid4())
        self.authz.grant_platform(uid, perms)
        return uid, self.tokens.user(uid, f"p-{uid[:6]}@nexus.local", roles=["platform_operator"])


@pytest.fixture
def harness(sdb, tokens):
    from analytics_service.main import create_app

    settings = Settings(
        env="test",
        database_url=sdb.app_url,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=TEST_AUDIENCE,
        redis_url="redis://127.0.0.1:1/0",
        outbox_relay_enabled=False,
        event_consumers_enabled=False,
        periodic_jobs_enabled=False,
    )
    store, celery = InMemoryObjectStore(), RecordingCelery()
    app = create_app(
        settings,
        platform_database=Database(sdb.url("platform"), pool_size=1, max_overflow=0),
        object_store=store,
        celery=celery,
    )
    app.state.token_verifier = tokens.verifier()
    authz = StaticAuthzResolver()
    app.state.authz_resolver = authz
    with TestClient(app) as client:
        app.state.redis = None
        yield Harness(client, app, authz, tokens, store, celery)

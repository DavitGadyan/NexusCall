"""Integration fixtures: one real PostgreSQL per session (restricted crm_app role + forced RLS), locally
signed JWTs, static authorization, in-memory object store and a recording task enqueuer. Requires Docker."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nexus_core.celery_app import JobContext
from nexus_core.db import Database
from nexus_core.storage import InMemoryObjectStore
from nexus_core.testing import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    StaticAuthzResolver,
    TokenFactory,
    auth,
    provision_and_migrate,
    start_postgres,
)

SERVICE_DIR = Path(__file__).resolve().parents[1]
ALL_PERMS = {
    "leads:read",
    "leads:create",
    "leads:update",
    "leads:export",
    "campaigns:read",
    "campaigns:create",
    "campaigns:manage",
}


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
    return provision_and_migrate(pg, "crm", SERVICE_DIR)


@pytest.fixture(scope="session")
def tokens():
    return TokenFactory()


@dataclass
class Harness:
    client: TestClient
    app: Any
    tokens: TokenFactory
    authz: StaticAuthzResolver
    store: InMemoryObjectStore
    app_url: str
    enqueued: list[tuple[str, str, JobContext, dict[str, str]]] = field(default_factory=list)

    def user(self, tenant_id: uuid.UUID, perms: set[str] | None = None, *, mfa: bool = False) -> dict[str, str]:
        sub = str(uuid.uuid4())
        self.authz.grant(sub, tenant_id, ALL_PERMS if perms is None else perms)
        return auth(self.tokens.user(sub, f"u-{sub[:8]}@example.test", mfa=mfa), tenant_id)

    def service(self, client_id: str, tenant_id: uuid.UUID | None, **extra: str) -> dict[str, str]:
        headers = auth(self.tokens.service(client_id), tenant_id, **extra)
        return headers

    def run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    def database(self) -> Database:
        return Database(self.app_url, pool_size=2, max_overflow=0)

    @staticmethod
    def ik() -> dict[str, str]:
        return {"Idempotency-Key": str(uuid.uuid4())}

    def job(self, fn: Any) -> Any:
        """Run `await fn(deps)` in ONE event loop; `deps` stands in for TaskRuntime (`.db`, `.store`)."""

        async def runner() -> Any:
            database = self.database()

            class _Deps:
                db = database
                store = self.store

            try:
                return await fn(_Deps())
            finally:
                await database.dispose()

        return asyncio.run(runner())


@pytest.fixture
def harness(sdb, tokens):
    from crm_service.main import create_app
    from crm_service.settings import Settings

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
    authz = StaticAuthzResolver()
    store = InMemoryObjectStore()
    recorded: list[tuple[str, str, JobContext, dict[str, str]]] = []

    async def enqueue(name: str, queue: str, ctx: JobContext, **ids: str) -> None:
        recorded.append((name, queue, ctx, ids))

    app = create_app(settings, authz_resolver=authz, object_store=store, enqueue=enqueue)
    app.state.token_verifier = tokens.verifier()
    with TestClient(app) as client:
        app.state.redis = None
        yield Harness(client, app, tokens, authz, store, sdb.app_url, recorded)


@pytest.fixture
def tenants():
    return uuid.uuid4(), uuid.uuid4()

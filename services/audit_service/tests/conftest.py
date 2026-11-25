"""Integration fixtures: one PostgreSQL container per session (restricted app role, forced RLS, the
read-only platform role), locally signed JWTs, static authz and an in-memory object store."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from audit_service.settings import Settings
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
    return provision_and_migrate(pg, "audit", SERVICE_DIR)


@pytest.fixture(scope="session")
def superuser_url(pg, sdb) -> str:
    return f"postgresql+asyncpg://{pg.username}:{pg.password}@{sdb.host}:{sdb.port}/audit"


@pytest.fixture(scope="session")
def tokens():
    return TokenFactory()


class Harness:
    def __init__(self, client: TestClient, app: Any, authz: StaticAuthzResolver, tokens: TokenFactory, store):
        self.client, self.app, self.authz, self.tokens, self.store = client, app, authz, tokens, store

    def member(self, tenant_id: uuid.UUID, perms: set[str], *, mfa: bool = False) -> tuple[str, str]:
        uid = str(uuid.uuid4())
        self.authz.grant(uid, tenant_id, perms)
        return uid, self.tokens.user(uid, f"u-{uid[:6]}@x.test", mfa=mfa)

    def platform_user(self, perms: set[str], *, mfa: bool = False) -> tuple[str, str]:
        uid = str(uuid.uuid4())
        self.authz.grant_platform(uid, perms)
        return uid, self.tokens.user(uid, f"p-{uid[:6]}@nexus.local", roles=["platform_operator"], mfa=mfa)


@pytest.fixture
def harness(sdb, tokens):
    settings = Settings(
        env="test",
        database_url=sdb.app_url,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=TEST_AUDIENCE,
        redis_url="redis://127.0.0.1:1/0",
        outbox_relay_enabled=False,
        event_consumers_enabled=False,
        periodic_jobs_enabled=False,
        audit_export_max_rows=5,
    )
    from audit_service.main import create_app

    store = InMemoryObjectStore()
    platform_database = Database(sdb.url("platform"), pool_size=1, max_overflow=0)
    app = create_app(settings, platform_database=platform_database, object_store=store)
    app.state.token_verifier = tokens.verifier()
    authz = StaticAuthzResolver()
    app.state.authz_resolver = authz
    with TestClient(app) as client:
        app.state.redis = None
        yield Harness(client, app, authz, tokens, store)

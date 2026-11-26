"""Integration fixtures: one real PostgreSQL per session (restricted integration_app role + forced RLS), Vault
mocked over HTTP with respx (an in-memory KV v2 engine), provider APIs mocked with respx, locally signed JWTs and
static authorization. No test reaches a real endpoint."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from integration_service.settings import Settings
from nexus_core.celery_app import JobContext
from nexus_core.testing import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    StaticAuthzResolver,
    TokenFactory,
    auth,
    provision_and_migrate,
    start_postgres,
)
from nexus_core.vault import VaultClient

SERVICE_DIR = Path(__file__).resolve().parents[1]
VAULT = "http://vault.test:8200"
VAULT_TOKEN = "hvs.testonlytoken0000000000000000"
SECRET = "test-fake-provider-secret"
os.environ["FAKE_PROVIDER_SECRET"] = SECRET  # read by the in-process simulators
SIM_BASE = "http://fake-providers.test"
PUBLIC = "https://app.nexus.test"
READ = {"integrations:read"}
MANAGE = {"integrations:read", "integrations:manage"}

# Realistic-looking credentials used across tests: every one of them must never leave Vault.
TWILIO = {"account_sid": "AC" + "0123456789abcdef" * 2, "auth_token": "fedcba9876543210" * 2}


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
    return provision_and_migrate(pg, "integration", SERVICE_DIR)


@pytest.fixture(scope="session")
def tokens():
    return TokenFactory()


# ------------------------------------------------------------------------------------------- mocked Vault
@dataclass
class FakeVault:
    """KV v2 semantics over respx: versioned writes, reads of the latest version, metadata, full delete."""

    data: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    destroyed: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    down: bool = False
    token_seen: set[str] = field(default_factory=set)

    def latest(self, path: str) -> dict[str, str] | None:
        versions = self.data.get(path)
        return versions[-1] if versions else None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503, json={"errors": ["sealed"]})
        self.token_seen.add(request.headers.get("x-vault-token", ""))
        if request.headers.get("x-vault-token") != VAULT_TOKEN:
            return httpx.Response(403, json={"errors": ["permission denied"]})
        m = re.match(r"^/v1/secret/(data|metadata)/(.+)$", request.url.path)
        if m is None:
            return httpx.Response(404, json={"errors": []})
        kind, path = m.groups()
        if kind == "data" and request.method == "POST":
            self.writes.append(path)
            self.data.setdefault(path, []).append(dict(json.loads(request.content)["data"]))
            return httpx.Response(200, json={"data": {"version": len(self.data[path])}})
        if kind == "data" and request.method == "GET":
            value = self.latest(path)
            if value is None:
                return httpx.Response(404, json={"errors": []})
            return httpx.Response(200, json={"data": {"data": value, "metadata": {"version": len(self.data[path])}}})
        if kind == "metadata" and request.method == "DELETE":
            self.destroyed.append(path)
            self.data.pop(path, None)
            return httpx.Response(204)
        if kind == "metadata" and request.method == "GET":
            if path not in self.data:
                return httpx.Response(404, json={"errors": []})
            return httpx.Response(200, json={"data": {"current_version": len(self.data[path]),
                                                      "created_time": "x", "updated_time": "y"}})  # fmt: skip
        return httpx.Response(405)


def make_settings(sdb: Any, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "test",
        "database_url": sdb.app_url,
        "oidc_issuer": TEST_ISSUER,
        "oidc_audience": TEST_AUDIENCE,
        "redis_url": "redis://127.0.0.1:1/0",
        "outbox_relay_enabled": False,
        "event_consumers_enabled": False,
        "periodic_jobs_enabled": False,
        "provider_mode": "real",
        "fake_provider_secret": SECRET,
        "fake_providers_url": SIM_BASE,
        "public_base_url": PUBLIC,
        "integration_hmac_key": "test-hmac-key-0123456789abcdef",
        "vault_addr": VAULT,
    }
    base.update(overrides)
    return Settings(**base)


class Harness:
    def __init__(self, client: TestClient, app: FastAPI, tokens: TokenFactory, authz: StaticAuthzResolver,
                 vault: FakeVault, mock: respx.MockRouter, published: list[tuple[JobContext, str]]) -> None:  # fmt: skip
        self.client, self.app, self.tokens, self.authz = client, app, tokens, authz
        self.vault, self.mock, self.published = vault, mock, published

    def user(self, tenant_id: uuid.UUID, permissions: set[str], *, mfa: bool = True) -> str:
        uid = uuid.uuid4()
        self.authz.grant(uid, tenant_id, permissions)
        return self.tokens.user(uid, f"u{uid.hex[:6]}@example.test", mfa=mfa)

    def platform_user(self, permissions: set[str], *, mfa: bool = True) -> str:
        uid = uuid.uuid4()
        self.authz.grant_platform(uid, permissions)
        return self.tokens.user(uid, f"p{uid.hex[:6]}@nexus.local", roles=["platform_owner"], mfa=mfa)

    def internal(self, tenant_id: uuid.UUID | None, client_id: str = "svc-call-service") -> dict[str, str]:
        return auth(self.tokens.service(client_id), tenant_id, **{"X-Actor": "service:test"})

    def run(self, coro: Any) -> Any:
        return self.client.portal.call(lambda: coro)  # type: ignore[union-attr]

    def sql(self, query: str, params: dict[str, Any] | None = None, *, tenant_id: uuid.UUID | None = None) -> Any:
        async def go() -> Any:
            database = self.app.state.db
            ctx = database.tenant_session(tenant_id) if tenant_id else database.global_session()
            async with ctx as s:
                result = await s.execute(text(query), params or {})
                return result.mappings().all() if result.returns_rows else None

        return self.run(go())

    def outbox(self, event_type: str) -> list[dict[str, Any]]:
        from integration_service.tables import outbox

        async def go() -> list[dict[str, Any]]:
            async with self.app.state.db.global_session() as s:
                rows = (await s.execute(select(outbox.c.envelope).where(outbox.c.event_type == event_type))).all()
            return [r.envelope for r in rows]

        return self.run(go())

    def dump_everything(self, tenant_ids: list[uuid.UUID] | None = None) -> str:
        """Every row of every table as JSON text, read as the PostgreSQL superuser (bypasses RLS, so rows of
        all tenants are included). Used to prove no secret value is ever persisted."""
        import asyncio

        import asyncpg

        pg = self.pg

        async def go() -> str:
            conn = await asyncpg.connect(user=pg.username, password=pg.password, host=pg.get_container_host_ip(),
                                         port=int(pg.get_exposed_port(5432)), database="integration")  # fmt: skip
            try:
                tables = [r["tablename"] for r in await conn.fetch(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")]  # fmt: skip
                chunks = []
                for t in tables:
                    chunks.append(await conn.fetchval(f"SELECT coalesce(json_agg(x)::text, '') FROM {t} x"))
                return "\n".join(chunks)
            finally:
                await conn.close()

        return asyncio.run(go())

    def create_twilio(self, tenant_id: uuid.UUID, *, status: int = 200, key: str | None = None,
                      creds: dict[str, str] | None = None) -> httpx.Response:  # fmt: skip
        creds = creds or TWILIO
        self.mock.get(f"https://api.twilio.com/2010-04-01/Accounts/{creds['account_sid']}.json").mock(
            return_value=httpx.Response(status, json={"sid": creds["account_sid"], "status": "active"})
        )
        user = self.user(tenant_id, MANAGE)
        return self.client.post(
            "/api/v1/integrations/connections",
            headers=auth(user, tenant_id, **{"Idempotency-Key": key or uuid.uuid4().hex}),
            json={"provider": "twilio", "credentials": creds},
        )


@pytest.fixture
def harness(pg, sdb, tokens) -> Iterator[Harness]:
    yield from _harness(pg, sdb, tokens)


def _harness(pg: Any, sdb: Any, tokens: TokenFactory, **overrides: Any) -> Iterator[Harness]:
    from integration_service.main import create_app

    settings = make_settings(sdb, **overrides.pop("settings", {}))
    authz = StaticAuthzResolver()
    published: list[tuple[JobContext, str]] = []

    async def publish(ctx: JobContext, delivery_id: str) -> None:
        published.append((ctx, delivery_id))

    fake_vault = FakeVault()
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        mock.route(url__startswith=VAULT).mock(side_effect=fake_vault.handler)
        vault = VaultClient(VAULT, token=VAULT_TOKEN)
        app = create_app(settings, authz_resolver=authz, vault=vault, publisher=publish, **overrides)
        app.state.token_verifier = tokens.verifier()
        with TestClient(app, base_url=PUBLIC) as client:
            app.state.redis = None
            h = Harness(client, app, tokens, authz, fake_vault, mock, published)
            h.pg = pg  # type: ignore[attr-defined]
            yield h


@pytest.fixture
def tenant() -> uuid.UUID:
    return uuid.uuid4()


def simulator_app() -> FastAPI:
    from fake_providers.routers import google_calendar, hubspot

    google_calendar.STATE.reset()
    hubspot.STATE.reset()
    sim = FastAPI()
    sim.include_router(google_calendar.router)
    sim.include_router(hubspot.router)
    return sim


@pytest.fixture
def fake_harness(pg, sdb, tokens) -> Iterator[Harness]:
    """Fake provider mode: OAuth and connection tests run against the in-process simulators."""
    sim = simulator_app()
    transport = httpx.ASGITransport(app=sim)
    for h in _harness(pg, sdb, tokens, settings={"provider_mode": "fake"}, egress_transport=transport):
        h.simulator = sim  # type: ignore[attr-defined]
        h.sim_transport = transport  # type: ignore[attr-defined]
        yield h

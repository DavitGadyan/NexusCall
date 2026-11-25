"""Integration fixtures: one real PostgreSQL per session (restricted mcpreg_app role, forced RLS), locally
signed JWTs, static authorization, ai-agent-service mocked over HTTP, deterministic DNS. Requires Docker."""

from __future__ import annotations

import asyncio
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from nexus_core.db import Database
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
FAKE_DNS = {
    "mcp.vendor.example": "93.184.216.34",
    "docs.vendor.example": "93.184.216.35",
    "rebind.vendor.example": "10.0.0.7",
    "metadata.vendor.example": "169.254.169.254",
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
    return provision_and_migrate(pg, "mcpreg", SERVICE_DIR)


@pytest.fixture(scope="session")
def tokens():
    return TokenFactory()


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    real = socket.getaddrinfo

    def resolver(host, port, *args, **kwargs):
        if isinstance(host, str) and host in FAKE_DNS:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (FAKE_DNS[host], port or 443))]
        if isinstance(host, str) and host.endswith(".example"):
            raise socket.gaierror("unknown test host")
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolver)


@dataclass
class Harness:
    client: TestClient
    app: Any
    tokens: TokenFactory
    authz: StaticAuthzResolver
    app_url: str
    agents: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    def user(self, tenant_id: uuid.UUID | None, perms: set[str], *, mfa: bool = False) -> dict[str, str]:
        sub = str(uuid.uuid4())
        if tenant_id is not None:
            self.authz.grant(sub, tenant_id, perms)
        return auth(self.tokens.user(sub, f"u-{sub[:8]}@example.test", mfa=mfa), tenant_id)

    def admin(self, perms: set[str] | None = None, *, mfa: bool = True) -> dict[str, str]:
        sub = str(uuid.uuid4())
        self.authz.grant_platform(sub, perms if perms is not None else {"platform:ai:read", "platform:ai:manage"})
        return auth(self.tokens.user(sub, f"admin-{sub[:8]}@nexus.local", roles=["platform_owner"], mfa=mfa))

    def service(self, client_id: str, tenant_id: uuid.UUID | None, **extra: str) -> dict[str, str]:
        return auth(self.tokens.service(client_id), tenant_id, **extra)

    def add_agent(self, tenant_id: uuid.UUID, agent_id: uuid.UUID | None = None, status: str = "active") -> uuid.UUID:
        agent_id = agent_id or uuid.uuid4()
        self.agents[(str(tenant_id), str(agent_id))] = {
            "agent_id": str(agent_id),
            "name": "Test agent",
            "kind": "voice",
            "status": status,
        }
        return agent_id

    def database(self) -> Database:
        return Database(self.app_url, pool_size=1, max_overflow=0)

    def sql(self, fn: Any) -> Any:
        async def runner() -> Any:
            database = self.database()
            try:
                return await fn(database)
            finally:
                await database.dispose()

        return asyncio.run(runner())


@pytest.fixture
def harness(sdb, tokens):
    from mcp_registry.main import create_app
    from mcp_registry.settings import Settings

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
    app = create_app(settings, authz_resolver=authz)
    app.state.token_verifier = tokens.verifier()

    async def svc_token() -> str:
        return tokens.service("svc-mcp-registry")

    h: Harness | None = None
    with respx.mock(assert_all_called=False) as mock:

        def agent_lookup(request):
            agent_id = request.url.path.rsplit("/", 1)[-1]
            tenant = request.headers.get("x-tenant-id", "")
            found = h.agents.get((tenant, agent_id)) if h else None
            return Response(200, json=found) if found else Response(404, json={"code": "not_found"})

        mock.get(url__regex=r"http://ai-agent-service:8000/internal/v1/agents/[^/]+$").mock(side_effect=agent_lookup)
        with TestClient(app) as client:
            app.state.service_token = svc_token
            app.state.redis = None
            h = Harness(client, app, tokens, authz, sdb.app_url)
            yield h

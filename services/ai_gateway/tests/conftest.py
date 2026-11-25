"""AI Gateway test harness.

* Real PostgreSQL (restricted `aigw_app` role, forced RLS) and real Redis (rate limits, breakers, slots).
* The REAL Anthropic adapter (official SDK) talks in-process to the fake Messages API via an ASGI transport.
* tenant-service, mcp-registry and tool-execution are mocked over HTTP with respx.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx2
import pytest
import respx
from anthropic import DefaultAsyncHttpxClient
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from redis.asyncio import Redis
from sqlalchemy import text

from ai_gateway.providers.anthropic_provider import AnthropicProvider
from ai_gateway.providers.base import ProviderRequest
from ai_gateway.settings import Settings
from nexus_core.db import Database
from nexus_core.testing import TEST_AUDIENCE, TEST_ISSUER, StaticAuthzResolver, TokenFactory, provision_and_migrate

SERVICE_DIR = Path(__file__).resolve().parents[1]
TENANT_A = uuid.UUID("0a000000-0000-4000-8000-0000000000aa")
TENANT_B = uuid.UUID("0a000000-0000-4000-8000-0000000000bb")
AGENT = uuid.UUID("0b000000-0000-4000-8000-000000000001")
CONTACT = uuid.UUID("0c000000-0000-4000-8000-000000000001")

TOOLS = {
    "knowledge.search": {
        "risk_class": 0,
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "maxLength": 500}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "booking.find_slots": {
        "risk_class": 0,
        "input_schema": {
            "type": "object",
            "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
            "required": ["from", "to"],
            "additionalProperties": False,
        },
    },
    "booking.create_tentative": {
        "risk_class": 2,
        "input_schema": {
            "type": "object",
            "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
            "required": ["start", "end"],
            "additionalProperties": False,
        },
    },
    "booking.confirm": {
        "risk_class": 3,
        "input_schema": {
            "type": "object",
            "properties": {"booking_id": {"type": "string"}},
            "required": ["booking_id"],
            "additionalProperties": False,
        },
    },
    "platform.manage_users": {"risk_class": 4, "input_schema": {"type": "object", "properties": {}}},
}


@pytest.fixture(scope="session")
def pg():
    from nexus_core.testing import start_postgres

    try:
        container = start_postgres()
    except Exception as exc:  # noqa: BLE001 - skip when Docker is unavailable
        pytest.skip(f"Docker unavailable: {exc}")
    yield container
    container.stop()


@pytest.fixture(scope="session")
def redis_url():
    tc = pytest.importorskip("testcontainers.redis")
    try:
        container = tc.RedisContainer("redis:7.4.1-alpine")
        container.start()
    except Exception as exc:  # noqa: BLE001 - skip when Docker is unavailable
        pytest.skip(f"Docker unavailable: {exc}")
    yield f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/0"
    container.stop()


@pytest.fixture(scope="session")
def sdb(pg):
    return provision_and_migrate(pg, "aigw", SERVICE_DIR)


@pytest.fixture(scope="session")
def tokens():
    return TokenFactory()


class RecordingProvider(AnthropicProvider):
    """The real adapter, recording exactly what would be sent to the provider."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.sent: list[ProviderRequest] = []
        self.fail_with: Exception | None = None

    async def complete(self, api_key: str, req: ProviderRequest):  # type: ignore[no-untyped-def]
        self.sent.append(req)
        if self.fail_with is not None:
            raise self.fail_with
        return await super().complete(api_key, req)

    def stream(self, api_key: str, req: ProviderRequest):  # type: ignore[no-untyped-def]
        self.sent.append(req)
        return super().stream(api_key, req)

    def sent_text(self) -> str:
        return "\n".join(r.system + json.dumps(r.messages) for r in self.sent)


def _fake_app() -> FastAPI:
    from fake_providers.routers.anthropic import router

    app = FastAPI()
    app.include_router(router)
    return app


class Harness:
    def __init__(
        self,
        client: TestClient,
        provider: RecordingProvider,
        mock: respx.MockRouter,
        tokens: TokenFactory,
        sdb: Any,
        tenant_ai: dict[str, Any],
        grants: list[str],
        tool_results: list[dict[str, Any]],
        executed: list[dict[str, Any]],
    ) -> None:
        self.client, self.provider, self.mock, self.tokens, self.sdb = client, provider, mock, tokens, sdb
        self.tenant_ai, self.grants, self.tool_results, self.executed = tenant_ai, grants, tool_results, executed

    def headers(self, tenant: uuid.UUID | None = TENANT_A, caller: str = "svc-voice-service") -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.tokens.service(caller)}", "X-Actor": "system:test"}
        if tenant:
            h["X-Tenant-ID"] = str(tenant)
        return h

    def respond(self, body: dict[str, Any], **kw: Any):  # type: ignore[no-untyped-def]
        return self.client.post("/internal/v1/ai/responses", json=body, headers=self.headers(**kw))

    def rows(self, sql: str, tenant: uuid.UUID = TENANT_A) -> list[dict[str, Any]]:
        async def q() -> list[dict[str, Any]]:
            db = Database(self.sdb.app_url, pool_size=1)
            async with db.tenant_session(tenant) as s:
                out = [dict(r) for r in (await s.execute(text(sql))).mappings().all()]
            await db.dispose()
            return out

        return asyncio.run(q())

    def owner_exec(self, sql: str, tenant: uuid.UUID | None = None) -> None:
        """Runs as the owner role. Forced RLS applies to the owner too, so tenant rows need a tenant context."""

        async def q() -> None:
            db = Database(self.sdb.owner_url, pool_size=1)
            async with db.engine.begin() as conn:
                if tenant is not None:
                    await conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
                await conn.execute(text(sql))
            await db.dispose()

        asyncio.run(q())


def body(
    text_: str = "Hello", *, source: str = "transcript", purpose: str = "voice_turn", **extra: Any
) -> dict[str, Any]:
    return {
        "purpose": purpose,
        "system": "You are the Acme receptionist. Be brief and polite.",
        "messages": [{"role": "user", "content": [{"text": text_, "source": source}]}],
        **extra,
    }


@pytest.fixture
def make_harness(sdb, tokens, redis_url):
    created: list[Any] = []

    def _make(**settings_overrides: Any) -> Harness:
        settings = Settings(
            env="test",
            provider_mode="fake",
            database_url=sdb.app_url,
            redis_url=redis_url,
            oidc_issuer=TEST_ISSUER,
            oidc_audience=TEST_AUDIENCE,
            outbox_relay_enabled=False,
            event_consumers_enabled=False,
            periodic_jobs_enabled=False,
            platform_database_url=sdb.url("platform"),
            **settings_overrides,
        )
        provider = RecordingProvider(
            "http://fake-providers/anthropic",
            max_retries=0,
            http_client=DefaultAsyncHttpxClient(transport=httpx2.ASGITransport(app=_fake_app())),
        )
        from ai_gateway.main import create_app

        app = create_app(settings, providers={"anthropic": provider})
        app.state.token_verifier = tokens.verifier()
        authz = StaticAuthzResolver()
        app.state.authz_resolver = authz
        tenant_ai = {
            "allowed_models": ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"],
            "monthly_budget_usd": 200,
            "data_classification_max": "confidential",
            "effective_ai_enabled": True,
        }
        grants: list[str] = list(TOOLS)
        tool_results: list[dict[str, Any]] = []
        executed: list[dict[str, Any]] = []

        mock = respx.mock(assert_all_called=False)
        mock.start()
        mock.get(url__regex=r"http://tenant-service:8000/internal/v1/tenants/[^/]+/settings/ai").mock(
            side_effect=lambda req: Response(200, json=tenant_ai)
        )
        flags: dict[str, Any] = {"registry_status": 200}
        mock.get(url__regex=r"http://mcp-registry:8000/internal/v1/agents/[^/]+/tools").mock(
            side_effect=lambda req: Response(503)
            if flags["registry_status"] != 200
            else Response(
                200,
                json={"tools": [{"tool_id": t, "version": 1, "description": f"{t} tool", **TOOLS[t]} for t in grants]},
            )
        )

        def execute(req: Any) -> Response:
            payload = json.loads(req.content)
            executed.append(
                {
                    **payload,
                    "headers": {
                        k: v for k, v in req.headers.items() if k in ("x-tenant-id", "x-actor", "idempotency-key")
                    },
                }
            )
            if tool_results:
                return Response(200, json=tool_results.pop(0))
            if payload["tool_id"] == "booking.find_slots":
                return Response(
                    200,
                    json={
                        "status": "executed",
                        "result": {"slots": [{"start": "2026-12-01T10:00:00Z", "end": "2026-12-01T10:30:00Z"}]},
                    },
                )
            return Response(200, json={"status": "executed", "result": {"ok": True}})

        mock.post("http://tool-execution:8000/internal/v1/tools/execute").mock(side_effect=execute)
        client = TestClient(app)
        client.__enter__()

        async def svc_token() -> str:
            return tokens.service("svc-ai-gateway")

        app.state.service_token = svc_token
        app.state.authz_static = authz
        created.append((client, mock, app))
        h = Harness(client, provider, mock, tokens, sdb, tenant_ai, grants, tool_results, executed)
        h.flags = flags  # type: ignore[attr-defined]
        h.app = app  # type: ignore[attr-defined]
        h.authz = authz  # type: ignore[attr-defined]
        return h

    yield _make
    for client, mock, _app in created:
        client.__exit__(None, None, None)
        mock.stop()

    async def flush() -> None:
        r = Redis.from_url(redis_url)
        await r.flushdb()
        await r.aclose()

    asyncio.run(flush())


@pytest.fixture
def h(make_harness):
    return make_harness()

"""Integration fixtures: one real PostgreSQL per session (restricted campaign_app role + forced RLS), locally
signed JWTs, static authorization and respx fakes for ai-agent, crm, tenant and call services."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from nexus_core.celery_app import JobContext
from nexus_core.clients import ServiceClient
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
ALL_PERMS = {"campaigns:read", "campaigns:create", "campaigns:manage", "campaigns:launch"}
DEFAULT_COMM = {
    "calls_enabled": True,
    "effective_calls_enabled": True,
    "calling_window": {"start": "00:00", "end": "23:59", "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
    "max_concurrent_calls": 20,
    "max_daily_calls": 1000,
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
    return provision_and_migrate(pg, "campaign", SERVICE_DIR)


@pytest.fixture(scope="session")
def tokens():
    return TokenFactory()


@dataclass
class Remote:
    """State behind the respx fakes of the other services."""

    agent: dict[str, Any] = field(default_factory=lambda: {"name": "Voice", "kind": "voice", "status": "active"})
    comm: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_COMM))
    audience: list[uuid.UUID] = field(default_factory=list)
    segment_missing: bool = False
    blocked: dict[str, list[str]] = field(default_factory=dict)  # contact id -> crm reasons
    call_status: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)  # contact id -> response
    calls: list[dict[str, Any]] = field(default_factory=list)
    canceled: list[str] = field(default_factory=list)
    crm_down: bool = False


def _install(mock: respx.MockRouter, remote: Remote) -> None:
    def agent(request: httpx.Request) -> httpx.Response:
        agent_id = request.url.path.rsplit("/", 1)[-1]
        if remote.agent.get("missing"):
            return httpx.Response(404, json={"code": "not_found"})
        return httpx.Response(200, json={"agent_id": agent_id, **remote.agent})

    def resolve(request: httpx.Request) -> httpx.Response:
        if remote.crm_down:
            return httpx.Response(503, json={"code": "unavailable"})
        if remote.segment_missing:
            return httpx.Response(404, json={"code": "not_found"})
        body = json.loads(request.content)
        start = int(base64.b64decode(body["cursor"]).decode()) if body.get("cursor") else 0
        page = remote.audience[start : start + body["limit"]]
        end = start + len(page)
        nxt = base64.b64encode(str(end).encode()).decode() if end < len(remote.audience) else None
        return httpx.Response(200, json={"items": [{"contact_id": str(i)} for i in page], "next_cursor": nxt})

    def batch(request: httpx.Request) -> httpx.Response:
        if remote.crm_down:
            return httpx.Response(503, json={"code": "unavailable"})
        ids = json.loads(request.content)["contact_ids"]
        items = []
        for cid in ids:
            reasons = remote.blocked.get(cid, [])
            items.append({"contact_id": cid, "allowed": not reasons, "reasons": reasons})
        return httpx.Response(200, json={"items": items})

    def settings(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=remote.comm)

    def create_call(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        remote.calls.append(
            {
                **body,
                "idempotency_key": request.headers.get("Idempotency-Key"),
                "tenant": request.headers.get("X-Tenant-ID"),
            }
        )
        status, payload = remote.call_status.get(body["contact_id"], (201, {}))
        if status == 201:
            payload = {
                "call_id": str(uuid.uuid5(uuid.NAMESPACE_URL, request.headers["Idempotency-Key"])),
                "state": "queued",
            }
        return httpx.Response(status, json=payload)

    def cancel_call(request: httpx.Request) -> httpx.Response:
        call_id = request.url.path.split("/")[-2]
        remote.canceled.append(call_id)
        return httpx.Response(200, json={"call_id": call_id, "state": "canceled"})

    mock.get(url__regex=r"^http://ai-agent-service:8000/internal/v1/agents/[^/]+$").mock(side_effect=agent)
    mock.post(url__regex=r"^http://crm-service:8000/internal/v1/segments/[^/]+/resolve$").mock(side_effect=resolve)
    mock.post("http://crm-service:8000/internal/v1/contacts/batch").mock(side_effect=batch)
    mock.get(url__regex=r"^http://tenant-service:8000/internal/v1/tenants/[^/]+/settings/communication$").mock(
        side_effect=settings
    )
    mock.post("http://call-service:8000/internal/v1/calls").mock(side_effect=create_call)
    mock.post(url__regex=r"^http://call-service:8000/internal/v1/calls/[^/]+/cancel$").mock(side_effect=cancel_call)


@dataclass
class Harness:
    client: TestClient
    app: Any
    tokens: TokenFactory
    authz: StaticAuthzResolver
    remote: Remote
    app_url: str
    enqueued: list[tuple[str, str, JobContext, dict[str, str]]]

    def user(self, tenant_id: uuid.UUID, perms: set[str] | None = None, *, mfa: bool = False) -> dict[str, str]:
        sub = str(uuid.uuid4())
        self.authz.grant(sub, tenant_id, ALL_PERMS if perms is None else perms)
        return auth(self.tokens.user(sub, f"u-{sub[:8]}@example.test", mfa=mfa), tenant_id)

    def service(self, client_id: str, tenant_id: uuid.UUID | None) -> dict[str, str]:
        return auth(self.tokens.service(client_id), tenant_id)

    @staticmethod
    def ik() -> dict[str, str]:
        return {"Idempotency-Key": str(uuid.uuid4())}

    def job(self, fn: Any) -> Any:
        """Run `await fn(deps)` in ONE event loop; deps stands in for TaskRuntime (`.db`, `.redis`, `.client`)."""
        tokens = self.tokens

        async def token() -> str:
            return tokens.service("svc-campaign-service")

        async def runner() -> Any:
            database = Database(self.app_url, pool_size=2, max_overflow=0)

            class _Deps:
                db = database
                redis = None

                @staticmethod
                def client(name: str) -> ServiceClient:
                    return ServiceClient(name, f"http://{name}:8000", token)

            try:
                return await fn(_Deps())
            finally:
                await database.dispose()

        return asyncio.run(runner())


@pytest.fixture
def harness(sdb, tokens):
    from campaign_service.main import create_app
    from campaign_service.settings import Settings

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
    recorded: list[tuple[str, str, JobContext, dict[str, str]]] = []

    async def enqueue(name: str, queue: str, ctx: JobContext, **ids: str) -> None:
        recorded.append((name, queue, ctx, ids))

    app = create_app(settings, authz_resolver=authz, enqueue=enqueue)
    app.state.token_verifier = tokens.verifier()
    remote = Remote()

    async def svc_token() -> str:
        return tokens.service("svc-campaign-service")

    with respx.mock(assert_all_called=False) as mock:
        _install(mock, remote)
        with TestClient(app) as client:
            app.state.redis = None
            app.state.service_token = svc_token
            yield Harness(client, app, tokens, authz, remote, sdb.app_url, recorded)


@pytest.fixture
def tenants():
    return uuid.uuid4(), uuid.uuid4()

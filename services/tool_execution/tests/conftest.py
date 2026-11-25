"""Integration fixtures: real PostgreSQL (restricted toolexec_app role, forced RLS), locally signed JWTs, static
authorization, an in-memory Redis stand-in, a fake Vault, and respx mocks for mcp-registry, the owning
services (crm, booking, call, ai-agent) and a streamable-HTTP MCP server. Requires Docker (one container)."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import SecretStr

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
from nexus_core.vault import VaultNotFound

sys.path.insert(0, str(Path(__file__).resolve().parent))

from toolexec_support import (
    FAKE_DNS,
    MCP_TOKEN,
    SERVICE_DIR,
    TOOLS,
    VAULT_REF,
    spec_for,
)


class MemoryRedis:
    """Just enough of redis.asyncio for rate limits, slots, breakers, quarantine and UI hints."""

    def __init__(self) -> None:
        self.kv: dict[str, Any] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.published: list[tuple[str, str]] = []

    async def get(self, key: str) -> Any:
        return self.kv.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None, nx: bool = False) -> bool:
        if nx and key in self.kv:
            return False
        self.kv[key] = str(value).encode() if not isinstance(value, bytes) else value
        return True

    async def incr(self, key: str) -> int:
        value = int(self.kv.get(key, b"0")) + 1
        self.kv[key] = str(value).encode()
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.kv.pop(k, None) is not None)

    async def zrem(self, key: str, member: str) -> int:
        return 1 if self.zsets.get(key, {}).pop(member, None) is not None else 0

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        if "ZCARD" in script:  # nexus_core.ratelimit acquire_slot
            key, now, ttl, max_slots, member = args[0], float(args[1]), float(args[2]), int(args[3]), args[4]
            zset = self.zsets.setdefault(key, {})
            for m, exp in list(zset.items()):
                if exp <= now:
                    zset.pop(m)
            if member in zset or len(zset) < max_slots:
                zset[member] = now + ttl
                return 1
            return 0
        return 0

    async def aclose(self) -> None:
        return None


class FakeVault:
    def __init__(self) -> None:
        self.reads: list[str] = []
        self.secrets = {VAULT_REF: {"token": SecretStr(MCP_TOKEN)}}

    async def read(self, path: str) -> dict[str, SecretStr]:
        self.reads.append(path)
        if path not in self.secrets:
            raise VaultNotFound("Secret not found", 404)
        return self.secrets[path]


@dataclass
class McpServer:
    name: str = "vendor-docs"
    version: str = "1.4.2"
    style: str = "json"  # json | sse
    text: str = "Result: the warranty lasts 24 months."
    structured: dict[str, Any] | None = None
    status: int = 200
    redirect: str | None = None
    oversize: bool = False
    echo_token: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)

    def handle(self, request: Any) -> Response:
        message = json.loads(request.content)
        self.calls.append({"message": message, "headers": dict(request.headers)})
        if self.redirect:
            return Response(307, headers={"location": self.redirect})
        if self.status != 200:
            return Response(self.status)
        method = message.get("method")
        if method == "notifications/initialized":
            return Response(202)
        if method == "initialize":
            result: Any = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                           "serverInfo": {"name": self.name, "version": self.version}}  # fmt: skip
        else:
            text = self.text
            if self.echo_token:
                text += " " + request.headers.get("authorization", "").removeprefix("Bearer ")
            if self.oversize:
                text = "x" * 20_000
            result = {"content": [{"type": "text", "text": text}], "isError": False}
            if self.structured is not None:
                result["structuredContent"] = self.structured
        reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        if self.style == "sse":
            body = f"event: message\ndata: {json.dumps(reply)}\n\n"
            return Response(200, text=body, headers={"content-type": "text/event-stream", "mcp-session-id": "sess-1"})
        return Response(200, json=reply, headers={"mcp-session-id": "sess-1"})


@dataclass
class Harness:
    client: TestClient
    app: Any
    tokens: TokenFactory
    authz: StaticAuthzResolver
    app_url: str
    redis: MemoryRedis
    vault: FakeVault
    mcp: McpServer
    specs: dict[str, dict[str, Any]]
    calls_db: dict[str, dict[str, Any]]
    downstream: list[dict[str, Any]]
    registry_calls: list[dict[str, Any]]
    flags: dict[str, Any]

    def grant(self, tool_id: str, **over: Any) -> None:
        self.specs[tool_id] = spec_for(tool_id, **over)

    def execute(
        self,
        tenant_id: uuid.UUID,
        tool_id: str,
        arguments: dict[str, Any] | None = None,
        *,
        agent_id: uuid.UUID,
        context: dict[str, Any] | None = None,
        key: str | None = None,
        actor: str = "service:voice-service",
        client_id: str = "svc-ai-gateway",
        extra: dict[str, Any] | None = None,
    ) -> Any:
        key = key or f"{uuid.uuid4()}:toolu_{uuid.uuid4().hex[:8]}"
        body = {
            "tool_id": tool_id,
            "arguments": arguments or {},
            "agent_id": str(agent_id),
            "context": {k: str(v) for k, v in (context or {}).items()},
            "idempotency_key": key,
            **(extra or {}),
        }
        headers = auth(self.tokens.service(client_id), tenant_id, **{"X-Actor": actor, "Idempotency-Key": key})
        return self.client.post("/internal/v1/tools/execute", json=body, headers=headers)

    def user(
        self, tenant_id: uuid.UUID, perms: set[str], *, mfa: bool = True, sub: str | None = None
    ) -> dict[str, str]:
        sub = sub or str(uuid.uuid4())
        self.authz.grant(sub, tenant_id, perms)
        return auth(self.tokens.user(sub, f"u-{sub[:8]}@example.test", mfa=mfa), tenant_id)

    def sql(self, fn: Any) -> Any:
        async def runner() -> Any:
            database = Database(self.app_url, pool_size=1, max_overflow=0)
            try:
                return await fn(database)
            finally:
                await database.dispose()

        return asyncio.run(runner())

    def downstream_to(self, fragment: str) -> list[dict[str, Any]]:
        return [d for d in self.downstream if fragment in d["url"]]


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
    return provision_and_migrate(pg, "toolexec", SERVICE_DIR)


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


@pytest.fixture
def harness(sdb, tokens):
    from tool_execution.main import create_app
    from tool_execution.settings import Settings

    settings = Settings(
        env="test",
        database_url=sdb.app_url,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=TEST_AUDIENCE,
        redis_url="redis://127.0.0.1:1/0",
        outbox_relay_enabled=False,
        event_consumers_enabled=False,
        periodic_jobs_enabled=False,
        mcp_breaker_failures=2,
    )
    authz = StaticAuthzResolver()
    vault = FakeVault()
    app = create_app(settings, authz_resolver=authz, vault=vault)
    app.state.token_verifier = tokens.verifier()
    specs: dict[str, dict[str, Any]] = {}
    calls_db: dict[str, dict[str, Any]] = {}
    downstream: list[dict[str, Any]] = []
    registry_calls: list[dict[str, Any]] = []
    flags: dict[str, Any] = {"registry_status": 200}
    mcp = McpServer()
    redis = MemoryRedis()

    async def svc_token() -> str:
        return tokens.service("svc-tool-execution")

    def registry(request):
        registry_calls.append({"method": request.method, "path": request.url.path})
        if flags["registry_status"] != 200:
            return Response(flags["registry_status"])
        tool_id = request.url.path.split("/")[4]
        spec = specs.get(tool_id)
        if spec is None:
            if tool_id in TOOLS:
                return Response(200, json=spec_for(tool_id, granted=False, reason="not_granted"))
            return Response(404, json={"code": "not_found"})
        return Response(200, json=spec)

    def record(service: str, payload: Any, status: int = 200):
        def handler(request):
            downstream.append(
                {
                    "service": service,
                    "method": request.method,
                    "url": str(request.url),
                    "headers": dict(request.headers),
                    "body": json.loads(request.content) if request.content else None,
                }
            )
            body = payload(request) if callable(payload) else payload
            return body if isinstance(body, Response) else Response(status, json=body)

        return handler

    def call_lookup(request):
        call_id = request.url.path.rsplit("/", 1)[-1]
        found = calls_db.get((request.headers.get("x-tenant-id"), call_id))
        return Response(200, json=found) if found else Response(404, json={"code": "not_found"})

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"http://mcp-registry:8000/internal/v1/tools/[^/]+/grant-check").mock(side_effect=registry)
        mock.get(url__regex=r"http://call-service:8000/internal/v1/calls/[^/]+$").mock(side_effect=call_lookup)
        mock.post(url__regex=r"http://call-service:8000/internal/v1/calls/[^/]+/transfer").mock(
            side_effect=record("call", {"status": "transferring"})
        )
        mock.post(url__regex=r"http://crm-service:8000/internal/v1/contacts/[^/]+/notes").mock(
            side_effect=record("crm", lambda r: Response(201, json={"note_id": str(uuid.uuid4())}))
        )
        mock.get(url__regex=r"http://crm-service:8000/internal/v1/contacts/[^/]+$").mock(
            side_effect=record(
                "crm",
                lambda r: {
                    "contact_id": r.url.path.rsplit("/", 1)[-1], "first_name": "Ada", "last_name": "Example",
                    "company": "Acme", "timezone": "UTC", "locale": "en", "status": "active", "tags": ["vip"],
                    "email_masked": "a***@example.test", "phone_masked": "+1555****01",
                    "custom_fields": {"internal_score": 99},
                },
            )
        )  # fmt: skip
        mock.post(url__regex=r"http://booking-service:8000/internal/v1/bookings/[^/]+/confirm").mock(
            side_effect=record("booking", lambda r: {"booking_id": r.url.path.split("/")[4], "status": "confirmed"})
        )
        mock.get(url__regex=r"http://booking-service:8000/internal/v1/availability.*").mock(
            side_effect=record("booking", {"slots": [{"start": "2026-12-01T10:00:00Z", "end": "2026-12-01T10:30:00Z",
                                                     "resource_id": None, "internal_note": "x"}]})
        )  # fmt: skip
        mock.post("http://ai-agent-service:8000/internal/v1/knowledge/search").mock(
            side_effect=record("agent", {"chunks": [{"chunk_id": "c1", "source_id": "s1", "source_name": "FAQ",
                                                     "text": "Open 9-17", "score": 0.9, "tenant_id": "leak"}]})
        )  # fmt: skip
        mock.post("https://mcp.vendor.example/mcp").mock(side_effect=mcp.handle)
        with TestClient(app) as client:
            app.state.service_token = svc_token
            app.state.redis = redis
            yield Harness(
                client, app, tokens, authz, sdb.app_url, redis, vault, mcp, specs, calls_db, downstream,
                registry_calls, flags,
            )  # fmt: skip


def now() -> float:
    return time.time()

"""Integration fixtures: ONE real PostgreSQL (pgvector) per session with the restricted `agent_app` role and
forced RLS, locally signed JWTs, static authorization, an in-memory object store, a tiny in-memory Redis, and
respx mocks of the AI Gateway and tenant-service (called exactly per BUILD_CONVENTIONS §5). Requires Docker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
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
GATEWAY = "http://ai-gateway:8000"
TENANT_SERVICE = "http://tenant-service:8000"
ALL_PERMS = {"ai:agents:read", "ai:agents:manage", "ai:usage:read"}
INJECTION = re.compile(r"ignore (all )?(previous|your) (instructions|rules)", re.I)


# ------------------------------------------------------------------------------------------- fakes
class FakeRedis:
    """Just enough of redis.asyncio for rate limits, tenant-settings caching and the failure counter."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.expiry: dict[str, float] = {}

    def _alive(self, key: str) -> bool:
        if key in self.expiry and self.expiry[key] < time.time():
            self.data.pop(key, None)
            self.expiry.pop(key, None)
        return key in self.data

    async def get(self, key: str) -> Any:
        return self.data[key] if self._alive(key) else None

    async def set(self, key: str, value: Any, ex: int | None = None, nx: bool = False) -> bool:
        if nx and self._alive(key):
            return False
        self.data[key] = value if isinstance(value, bytes) else str(value).encode()
        if ex:
            self.expiry[key] = time.time() + ex
        return True

    async def incr(self, key: str) -> int:
        value = int(self.data[key]) + 1 if self._alive(key) else 1
        self.data[key] = str(value).encode()
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        self.expiry[key] = time.time() + seconds
        return True

    async def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.data.pop(k, None) is not None)

    async def aclose(self) -> None:
        return None


def hash_embed(text: str, dims: int = 1024) -> list[float]:
    """Same deterministic token-hash embedding as the gateway's fake embedder."""
    vec = [0.0] * dims
    for token in text.lower().split():
        h = int.from_bytes(hashlib.sha256(token.encode()).digest()[:8], "big")
        vec[h % dims] += 1.0 if (h >> 63) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@dataclass
class FakeGateway:
    """Stands in for the AI Gateway's internal API. Records every request body it receives."""

    embedding_model: str = "voyage-3"
    analysis: dict[str, Any] = field(default_factory=lambda: {"risk": "low", "findings": ["none"]})
    triage: dict[str, Any] = field(
        default_factory=lambda: {"intent": "question", "priority": "normal", "sensitive": False}
    )
    search_query: str | None = None
    draft: dict[str, Any] | None = None  # None: cite the first knowledge chunk offered
    postprocess: dict[str, Any] = field(
        default_factory=lambda: {
            "summary": "Caller asked about prices.",
            "outcome": "interested",
            "qualified": True,
            "follow_up": None,
        }
    )
    eval_text: str = "Thanks for your message. A colleague will follow up."
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    deny: dict[str, str] = field(default_factory=dict)  # purpose -> reason
    unavailable: bool = False
    requests: list[dict[str, Any]] = field(default_factory=list)
    embeddings: list[dict[str, Any]] = field(default_factory=list)

    def _flags(self, body: dict[str, Any]) -> list[str]:
        texts = [b["text"] for m in body.get("messages", []) for b in m["content"] if b.get("trust") == "untrusted"]
        return ["prompt_injection_suspected"] if any(INJECTION.search(t) for t in texts) else []

    def responses(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"headers": dict(request.headers), **body})
        if self.unavailable:
            return httpx.Response(503, json={"code": "unavailable"})
        purpose = body["purpose"]
        if purpose in self.deny:
            return httpx.Response(403, json={"code": "policy_denied", "reason": self.deny[purpose]})
        text, parsed = "", None
        schema_props = (body.get("response_schema") or {}).get("properties", {})
        if purpose == "classification" and "risk" in schema_props:
            parsed = self.analysis
        elif purpose == "classification":
            parsed = self.triage
        elif purpose == "summary":
            first = body["messages"][0]["content"][0]["text"]
            parsed = {"summary": "Customer asks a question.", "search_query": self.search_query or first[:200]}
        elif purpose == "email_draft":
            if self.draft is not None:
                parsed = self.draft
            else:
                ids = [
                    m.group(1)
                    for b in body["messages"][0]["content"]
                    if b["source"] == "knowledge"
                    for m in [re.search(r"chunk_id: ([0-9a-f-]{36})", b["text"])]
                    if m
                ]
                parsed = {
                    "draft_text": "Our standard call-out visit costs $89." if ids else "A colleague will follow up.",
                    "factual_claims": bool(ids),
                    "cited_chunk_ids": ids[:1],
                    "confidence": 0.9,
                    "needs_human": False,
                }
        elif purpose == "call_postprocess":
            parsed = self.postprocess
        else:
            text = self.eval_text
        if parsed is not None:
            text = json.dumps(parsed)
        return httpx.Response(
            200,
            json={
                "request_id": str(uuid.uuid4()),
                "text": text,
                "json": parsed,
                "stop_reason": "end_turn",
                "tool_calls": self.tool_calls,
                "usage": {"input_tokens": 100, "output_tokens": 20, "cost_micros": 30},
                "model": "claude-sonnet-5-5",
                "provider": "fake",
                "flags": self._flags(body),
            },
        )

    def embed(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.embeddings.append({"headers": dict(request.headers), **body})
        if self.unavailable:
            return httpx.Response(503, json={"code": "unavailable"})
        if "embedding" in self.deny:
            return httpx.Response(403, json={"code": "policy_denied", "reason": self.deny["embedding"]})
        vectors = [hash_embed(t) for t in body["texts"]]
        return httpx.Response(200, json={"vectors": vectors, "model": self.embedding_model, "dims": 1024})

    def purposes(self) -> list[str]:
        return [r["purpose"] for r in self.requests]


@dataclass
class TenantPolicy:
    caps: dict[str, str] = field(default_factory=dict)

    def handler(self, request: httpx.Request) -> httpx.Response:
        tenant = request.url.path.split("/")[4]
        return httpx.Response(
            200,
            json={
                "allowed_models": [],
                "monthly_budget_usd": 100,
                "draft_auto_send": False,
                "data_classification_max": self.caps.get(tenant, "internal"),
                "store_prompt_content": False,
                "effective_ai_enabled": True,
            },
        )


# ------------------------------------------------------------------------------------------- fixtures
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
    return provision_and_migrate(pg, "agent", SERVICE_DIR, extensions=("pgcrypto", "btree_gist", "vector"))


@pytest.fixture(scope="session")
def tokens():
    return TokenFactory()


async def _token() -> str:
    return "test-service-token"


@dataclass
class JobDeps:
    db: Database
    store: Any
    redis: Any
    settings: Any

    def client(self, service: str, *, timeout: float = 10.0) -> ServiceClient:
        return ServiceClient(service, self.settings.service_url(service), _token, timeout=timeout)


@dataclass
class Harness:
    client: TestClient
    app: Any
    tokens: TokenFactory
    authz: StaticAuthzResolver
    store: InMemoryObjectStore
    redis: FakeRedis
    gateway: FakeGateway
    policy: TenantPolicy
    app_url: str
    enqueued: list[tuple[str, str, JobContext, dict[str, str]]] = field(default_factory=list)

    embed = staticmethod(hash_embed)

    @property
    def settings(self) -> Any:
        return self.app.state.settings

    def user(
        self,
        tenant_id: uuid.UUID,
        perms: set[str] | None = None,
        *,
        mfa: bool = False,
        roles: set[str] | None = None,
        sub: str | None = None,
    ) -> dict[str, str]:
        sub = sub or str(uuid.uuid4())
        self.authz.grant(sub, tenant_id, ALL_PERMS if perms is None else perms, roles=roles)
        return auth(self.tokens.user(sub, f"u-{sub[:8]}@example.test", mfa=mfa), tenant_id)

    def service(self, client_id: str, tenant_id: uuid.UUID | None, **extra: str) -> dict[str, str]:
        return auth(self.tokens.service(client_id), tenant_id, **extra)

    @staticmethod
    def ik() -> dict[str, str]:
        return {"Idempotency-Key": str(uuid.uuid4())}

    def database(self) -> Database:
        return Database(self.app_url, pool_size=2, max_overflow=0)

    def job(self, fn: Any) -> Any:
        """Run `await fn(deps)` in one event loop; `deps` stands in for TaskRuntime."""

        async def runner() -> Any:
            database = self.database()
            try:
                return await fn(JobDeps(database, self.store, self.redis, self.settings))
            finally:
                await database.dispose()

        return asyncio.run(runner())

    def sql(self, fn: Any) -> Any:
        async def runner() -> Any:
            database = self.database()
            try:
                return await fn(database)
            finally:
                await database.dispose()

        return asyncio.run(runner())

    def run_ingest(self, document_id: uuid.UUID | str, tenant_id: uuid.UUID) -> str:
        from ai_agent_service.knowledge import ingest_document

        ctx = JobContext(tenant_id=tenant_id, correlation_id=uuid.uuid4())
        return self.job(lambda deps: ingest_document(deps, ctx, uuid.UUID(str(document_id))))

    # ---- convenience builders
    def make_source(self, tenant_id: uuid.UUID, headers: dict[str, str] | None = None, **fields: Any) -> dict[str, Any]:
        headers = headers or self.user(tenant_id)
        body = {"name": f"src-{uuid.uuid4().hex[:8]}", **fields}
        resp = self.client.post("/api/v1/ai/knowledge/sources", json=body, headers={**headers, **self.ik()})
        assert resp.status_code == 201, resp.text
        return resp.json()

    def upload(
        self,
        tenant_id: uuid.UUID,
        source_id: str,
        content: bytes,
        filename: str = "faq.md",
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers = headers or self.user(tenant_id)
        return self.client.post(
            f"/api/v1/ai/knowledge/sources/{source_id}/documents",
            files={"file": (filename, content, "application/octet-stream")},
            headers={**headers, **self.ik()},
        )

    def indexed_document(
        self, tenant_id: uuid.UUID, text: str, *, source: dict[str, Any] | None = None, filename: str = "faq.md"
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        source = source or self.make_source(tenant_id)
        resp = self.upload(tenant_id, source["id"], text.encode(), filename)
        assert resp.status_code == 202, resp.text
        doc = resp.json()
        self.run_ingest(doc["id"], tenant_id)
        got = self.client.get(f"/api/v1/ai/knowledge/documents/{doc['id']}", headers=self.user(tenant_id))
        return source, got.json()

    def make_agent(
        self, tenant_id: uuid.UUID, kind: str = "email", *, activate: bool = True, **config: Any
    ) -> dict[str, Any]:
        headers = self.user(tenant_id)
        cfg = {"system_prompt": "You are a helpful assistant for a small business. Be concise and polite.", **config}
        resp = self.client.post(
            "/api/v1/ai/agents",
            json={"name": f"agent-{uuid.uuid4().hex[:8]}", "kind": kind, "config": cfg},
            headers={**headers, **self.ik()},
        )
        assert resp.status_code == 201, resp.text
        agent = resp.json()
        if activate:
            resp = self.client.post(f"/api/v1/ai/agents/{agent['id']}/versions/1/activate", headers=headers)
            assert resp.status_code == 200, resp.text
            agent = resp.json()
        return agent

    def outbox_events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from ai_agent_service.tables import outbox

        async def fetch(database: Database) -> list[dict[str, Any]]:
            async with database.global_session() as s:
                rows = (await s.execute(select(outbox.c.event_type, outbox.c.envelope))).all()
            return [r.envelope for r in rows if event_type is None or r.event_type == event_type]

        return self.sql(fetch)


@pytest.fixture
def harness(sdb, tokens):
    from ai_agent_service.main import create_app
    from ai_agent_service.settings import Settings

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

    gateway = FakeGateway()
    policy = TenantPolicy()
    fake_redis = FakeRedis()
    app = create_app(settings, authz_resolver=authz, object_store=store, enqueue=enqueue)
    app.state.token_verifier = tokens.verifier()
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        mock.post(f"{GATEWAY}/internal/v1/ai/responses").mock(side_effect=gateway.responses)
        mock.post(f"{GATEWAY}/internal/v1/ai/embeddings").mock(side_effect=gateway.embed)
        mock.get(url__regex=rf"{TENANT_SERVICE}/internal/v1/tenants/[^/]+/settings/ai").mock(side_effect=policy.handler)
        with TestClient(app) as client:
            app.state.redis = fake_redis
            app.state.service_token = _token
            yield Harness(client, app, tokens, authz, store, fake_redis, gateway, policy, sdb.app_url, recorded)


@pytest.fixture
def tenants():
    return uuid.uuid4(), uuid.uuid4()

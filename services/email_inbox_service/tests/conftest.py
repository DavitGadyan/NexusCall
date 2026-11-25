"""Integration fixtures: ONE real PostgreSQL per session (restricted `inbox_app` role, forced RLS), locally
signed JWTs, static authorization, an in-memory object store, a fake clamd on loopback, and respx mocks of
tenant-service, crm-service, email-service and ai-agent-service exactly per BUILD_CONVENTIONS §5.

Celery is not used: enqueued jobs are recorded and the task coroutines are run directly (`harness.drain()`).
"""

from __future__ import annotations

import asyncio
import json
import socketserver
import struct
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from email_inbox_service.deps import Deps
from email_inbox_service.drafting import generate_draft
from email_inbox_service.inbound import process_inbound_safe
from email_inbox_service.sending import send_approved
from email_inbox_service.settings import Settings
from email_inbox_service.tables import outbox
from fake_providers.routers import ses_inbound
from fake_providers.sns import cert_url, certificate_pem
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inbox_support import MANAGE, Job, draft_result

SERVICE_DIR = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------------------------------- fake clamd
class _Clamd(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        conn = self.request
        buf = b""
        while not buf.endswith(b"\0"):
            chunk = conn.recv(1)
            if not chunk:
                return
            buf += chunk
        data = b""
        while True:
            size = struct.unpack("!L", _recv_exact(conn, 4))[0]
            if size == 0:
                break
            data += _recv_exact(conn, size)
        conn.sendall(b"stream: Eicar-Test-Signature FOUND\0" if b"EICAR-STANDARD" in data else b"stream: OK\0")


def _recv_exact(conn: Any, n: int) -> bytes:
    out = b""
    while len(out) < n:
        chunk = conn.recv(n - len(out))
        if not chunk:
            raise ConnectionError("closed")
        out += chunk
    return out


@pytest.fixture(scope="session")
def clamd():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Clamd)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


# ------------------------------------------------------------------------------------------- database
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
    return provision_and_migrate(pg, "inbox", SERVICE_DIR)


@pytest.fixture(scope="session")
def tokens():
    return TokenFactory()


# ------------------------------------------------------------------------------------------- fake services
@dataclass
class FakeServices:
    authz: StaticAuthzResolver
    suppressed: set[str] = field(default_factory=set)
    emails_enabled: bool = True
    contacts: dict[str, str] = field(default_factory=dict)
    campaign_messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    agents: dict[str, dict[str, Any]] = field(default_factory=dict)
    draft: Callable[[dict[str, Any]], dict[str, Any]] = draft_result
    drafting_status: int = 200
    send_status: int = 202
    authz_down: bool = False
    crm_down: bool = False
    drafting_requests: list[dict[str, Any]] = field(default_factory=list)
    send_requests: list[dict[str, Any]] = field(default_factory=list)
    sent_by_key: dict[str, dict[str, Any]] = field(default_factory=dict)
    webhook_posts: list[bytes] = field(default_factory=list)

    # tenant-service
    def resolve(self, request: httpx.Request) -> httpx.Response:
        if self.authz_down:
            return httpx.Response(503, json={"code": "unavailable"})
        body = json.loads(request.content)
        access = self.authz.tenant.get((body["sub"], body["tenant_id"]))
        if access is None:
            return httpx.Response(
                200, json={"is_member": False, "permissions": [], "roles": [], "tenant_status": "active"}
            )
        return httpx.Response(
            200,
            json={
                "is_member": access.is_member,
                "permissions": sorted(access.permissions),
                "roles": sorted(access.roles),
                "tenant_status": access.tenant_status,
            },
        )

    def settings_section(self, request: httpx.Request, section: str) -> httpx.Response:
        if section == "communication":
            return httpx.Response(
                200, json={"emails_enabled": self.emails_enabled, "effective_emails_enabled": self.emails_enabled}
            )
        if section == "retention":
            return httpx.Response(
                200,
                json={"email_content_days": 30, "attachments_days": 30, "recordings_days": 90, "transcripts_days": 90},
            )
        return httpx.Response(404, json={"code": "not_found"})

    # crm-service
    def lookup(self, request: httpx.Request) -> httpx.Response:
        email = request.url.params.get("email", "")
        return httpx.Response(200, json={"contact_id": self.contacts.get(email)})

    def suppression(self, request: httpx.Request) -> httpx.Response:
        if self.crm_down:
            return httpx.Response(503, json={"code": "unavailable"})
        value = request.url.params.get("value", "").lower()
        hit = value in self.suppressed
        return httpx.Response(200, json={"suppressed": hit, "reasons": ["opt_out"] if hit else []})

    # email-service
    def message_lookup(self, request: httpx.Request) -> httpx.Response:
        found = self.campaign_messages.get(request.url.params.get("rfc_message_id", ""))
        return httpx.Response(200, json=found) if found else httpx.Response(404, json={"code": "not_found"})

    def send(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("idempotency-key")
        body = json.loads(request.content)
        self.send_requests.append({"key": key, "body": body, "headers": dict(request.headers)})
        if self.send_status >= 300:
            return httpx.Response(self.send_status, json={"code": "rejected" if self.send_status < 500 else "down"})
        if key in self.sent_by_key:  # idempotent replay: the same logical message
            return httpx.Response(202, json=self.sent_by_key[key])
        result = {"message_id": str(uuid.uuid4()), "state": "queued"}
        self.sent_by_key[key or ""] = result
        return httpx.Response(202, json=result)

    @property
    def delivered(self) -> int:
        """Distinct emails email-service accepted (idempotent replays do not count twice)."""
        return len(self.sent_by_key)

    # ai-agent-service
    def drafting(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.drafting_requests.append({"headers": dict(request.headers), **body})
        if self.drafting_status != 200:
            return httpx.Response(self.drafting_status, json={"code": "policy_denied", "reason": "ai_disabled"})
        return httpx.Response(200, json=self.draft(body))

    def agent(self, request: httpx.Request, agent_id: str) -> httpx.Response:
        found = self.agents.get(agent_id)
        return httpx.Response(200, json=found) if found else httpx.Response(404, json={"code": "not_found"})


def _mount(mock: respx.MockRouter, fake: FakeServices) -> None:
    mock.post("http://tenant-service:8000/internal/v1/authz/resolve").mock(side_effect=fake.resolve)
    mock.get(url__regex=r"^http://tenant-service:8000/internal/v1/tenants/[^/]+/settings/(?P<section>\w+)$").mock(
        side_effect=lambda request, section: fake.settings_section(request, section)
    )
    mock.get(url__regex=r"^http://crm-service:8000/internal/v1/contacts/lookup").mock(side_effect=fake.lookup)
    mock.get(url__regex=r"^http://crm-service:8000/internal/v1/suppressions/check").mock(side_effect=fake.suppression)
    mock.get(url__regex=r"^http://email-service:8000/internal/v1/messages/lookup").mock(side_effect=fake.message_lookup)
    mock.post("http://email-service:8000/internal/v1/send").mock(side_effect=fake.send)
    mock.post("http://ai-agent-service:8000/internal/v1/drafting/runs").mock(side_effect=fake.drafting)
    mock.get(url__regex=r"^http://ai-agent-service:8000/internal/v1/agents/(?P<agent_id>[^/]+)$").mock(
        side_effect=lambda request, agent_id: fake.agent(request, agent_id)
    )
    mock.get(cert_url()).mock(return_value=httpx.Response(200, content=certificate_pem()))
    mock.post("http://email-inbox-service:8000/webhooks/ses/inbound").mock(
        side_effect=lambda request: fake.webhook_posts.append(request.content) or httpx.Response(200, json={})
    )


# ------------------------------------------------------------------------------------------- harness
class Harness:
    def __init__(
        self,
        *,
        client: TestClient,
        app: FastAPI,
        sdb: Any,
        tokens: TokenFactory,
        authz: StaticAuthzResolver,
        fake: FakeServices,
        store: InMemoryObjectStore,
        jobs: list[Job],
        settings: Settings,
        simulator: TestClient,
    ) -> None:
        self.client, self.app, self.sdb, self.tokens, self.authz = client, app, sdb, tokens, authz
        self.fake, self.store, self.jobs, self.settings, self.simulator = fake, store, jobs, settings, simulator
        self.tenant_a, self.tenant_b = uuid.uuid4(), uuid.uuid4()

    # --- identities
    def user(self, tenant: uuid.UUID, perms: set[str], *, mfa: bool = True) -> tuple[str, dict[str, str]]:
        sub = str(uuid.uuid4())
        self.authz.grant(sub, tenant, perms)
        token = self.tokens.user(sub, f"{sub[:8]}@example.test", mfa=mfa)
        return sub, auth(token, tenant)

    def headers_for(self, sub: str, tenant: uuid.UUID, *, mfa: bool = True, **extra: str) -> dict[str, str]:
        return auth(self.tokens.user(sub, f"{sub[:8]}@example.test", mfa=mfa), tenant, **extra)

    # --- async helpers (fresh engine per event loop)
    def run(self, fn: Callable[[Deps], Any]) -> Any:
        async def _go() -> Any:
            db = Database(self.sdb.app_url, pool_size=2, max_overflow=0)

            async def token() -> str:
                return self.tokens.service("svc-email-inbox-service")

            async def enqueue(name: str, queue: str, ctx: JobContext, **ids: str) -> None:
                self.jobs.append(Job(name, ctx, ids))

            deps = Deps(
                settings=self.settings,
                db=db,
                redis=None,
                store=self.store,
                client=lambda name: ServiceClient(name, f"http://{name}:8000", token, timeout=10.0),
                enqueue=enqueue,
            )
            try:
                return await fn(deps)
            finally:
                await db.dispose()

        return asyncio.run(_go())

    def sql(self, statement: str, params: dict[str, Any] | None = None, *, tenant: uuid.UUID | None = None) -> Any:
        async def go(deps: Deps) -> Any:
            ctx = deps.db.tenant_session(tenant) if tenant else deps.db.global_session()
            async with ctx as s:
                result = await s.execute(text(statement), params or {})
                return result.mappings().all() if result.returns_rows else None

        return self.run(go)

    def events(self, event_type: str, tenant: uuid.UUID | None = None) -> list[dict[str, Any]]:
        async def go(deps: Deps) -> list[dict[str, Any]]:
            async with deps.db.global_session() as s:
                stmt = select(outbox.c.envelope).where(outbox.c.event_type == event_type)
                if tenant:
                    stmt = stmt.where(outbox.c.tenant_id == tenant)
                return list((await s.execute(stmt)).scalars().all())

        return self.run(go)

    def audits(self, action: str, tenant: uuid.UUID) -> list[dict[str, Any]]:
        return [e for e in self.events("audit.event", tenant) if e["payload"]["action"] == action]

    # --- jobs
    def drain(
        self, *, names: tuple[str, ...] = ("emails.inbound_process", "emails.ai_draft"), max_rounds: int = 20
    ) -> list[Any]:
        """Run queued jobs whose task name is in `names` (sending is opt-in), including follow-up jobs."""
        results = []
        for _ in range(max_rounds):
            ready = [j for j in self.jobs if j.name in names]
            if not ready:
                break
            for job in ready:
                self.jobs.remove(job)
                results.append(self.run_job(job))
        return results

    def run_job(self, job: Job) -> Any:
        coro = {
            "emails.inbound_process": lambda d: process_inbound_safe(d, job.ctx, uuid.UUID(job.ids["event_id"])),
            "emails.ai_draft": lambda d: generate_draft(d, job.ctx, uuid.UUID(job.ids["draft_id"])),
            "emails.send_approved": lambda d: send_approved(d, job.ctx, uuid.UUID(job.ids["command_id"])),
        }[job.name]
        return self.run(coro)

    def send_jobs(self) -> list[Job]:
        return [j for j in self.jobs if j.name == "emails.send_approved"]

    def run_sends(self) -> list[Any]:
        return self.drain(names=("emails.send_approved",))

    # --- inbound mail through the real simulator + real webhook
    def mailbox(self, tenant: uuid.UUID, *, auto_draft: bool = True) -> dict[str, Any]:
        _, headers = self.user(tenant, MANAGE)
        address = f"support-{uuid.uuid4().hex[:8]}@{str(tenant)[:8]}.example.test"
        resp = self.client.post(
            "/api/v1/email/mailboxes",
            headers=headers,
            json={"address": address, "display_name": "Support", "auto_draft": auto_draft},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    def envelope(self, **body: Any) -> dict[str, Any]:
        body.setdefault("sender", "customer@example.test")
        body.setdefault("subject", "Opening hours?")
        body.setdefault("text", "Hi, when are you open?")
        resp = self.simulator.post("/ses-inbound/simulate", json={**body, "deliver": False})
        assert resp.status_code == 200, resp.text
        return resp.json()["envelope"]

    def post_envelope(self, envelope: dict[str, Any], **headers: str) -> httpx.Response:
        return self.client.post(
            "/webhooks/ses/inbound", content=json.dumps(envelope), headers={"Content-Type": "text/plain", **headers}
        )

    def deliver(self, to: str | list[str], **body: Any) -> dict[str, Any]:
        envelope = self.envelope(to=[to] if isinstance(to, str) else to, **body)
        resp = self.post_envelope(envelope)
        assert resp.status_code == 200, resp.text
        self.drain()
        return envelope

    def only_draft(self, tenant: uuid.UUID, headers: dict[str, str]) -> dict[str, Any]:
        items = self.client.get("/api/v1/email/drafts", headers=headers).json()["items"]
        assert len(items) == 1, items
        return items[0]


@pytest.fixture
def harness(sdb, tokens, clamd):
    authz = StaticAuthzResolver()
    fake = FakeServices(authz=authz)
    store = InMemoryObjectStore()
    jobs: list[Job] = []
    settings = Settings(
        env="test",
        provider_mode="fake",
        database_url=sdb.app_url,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=TEST_AUDIENCE,
        redis_url="redis://127.0.0.1:1/0",
        outbox_relay_enabled=False,
        event_consumers_enabled=False,
        periodic_jobs_enabled=False,
        clamav_host="127.0.0.1",
        clamav_port=clamd,
        clamav_timeout_seconds=5.0,
    )
    from email_inbox_service.main import create_app

    async def enqueue(name: str, queue: str, ctx: JobContext, **ids: str) -> None:
        jobs.append(Job(name, ctx, ids))

    app = create_app(settings, authz_resolver=authz, object_store=store, enqueue=enqueue)
    app.state.token_verifier = tokens.verifier()

    async def svc_token() -> str:
        return tokens.service("svc-email-inbox-service")

    sim_app = FastAPI()
    sim_app.include_router(ses_inbound.router)
    sim_app.dependency_overrides[ses_inbound.get_store] = lambda: store

    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, fake)
        with TestClient(app) as client, TestClient(sim_app) as simulator:
            app.state.service_token = svc_token
            app.state.redis = None
            h = Harness(
                client=client,
                app=app,
                sdb=sdb,
                tokens=tokens,
                authz=authz,
                fake=fake,
                store=store,
                jobs=jobs,
                settings=settings,
                simulator=simulator,
            )
            h.mock = mock
            yield h

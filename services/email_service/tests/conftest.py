"""Integration fixtures: one real PostgreSQL per session (restricted `email_app` role + forced RLS), locally
signed JWTs, a static authorization resolver, respx fakes for tenant-service and crm-service, and test
doubles for the email provider and the rate limiter. No test reaches a real network endpoint."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from email_service.providers.dns import FakeDnsVerifier
from email_service.providers.ses import AmbiguousSendError, fake_dkim_tokens
from email_service.settings import FAKE_SES_EVENTS_TOPIC, Settings
from fake_providers.sns import certificate_pem, sign_sns
from nexus_core.celery_app import JobContext
from nexus_core.clients import ServiceClient
from nexus_core.credentials import CredentialResolver
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
from nexus_providers.email import PermanentSendError, SendResult, TransientSendError

SERVICE_DIR = Path(__file__).resolve().parents[1]
MANAGE = "emails:campaigns:manage"
ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
INBOX_CLIENT = "svc-email-inbox-service"


def comm_settings(**overrides: Any) -> dict[str, Any]:
    return {
        "emails_enabled": True,
        "effective_emails_enabled": True,
        "email_window": {"start": "00:00", "end": "23:59", "days": ALL_DAYS},
        "email_daily_quota": 5000,
        **overrides,
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
    return provision_and_migrate(pg, "email", SERVICE_DIR)


@pytest.fixture(scope="session")
def tokens():
    return TokenFactory()


# --------------------------------------------------------------------------------------------- test doubles
class FakeProvider:
    """Deterministic stand-in for the outbound adapter. `mode` injects failures for the next sends."""

    name = "fake"

    def __init__(self) -> None:
        self.supports_lookup = True
        self.sent: list[Any] = []
        self.accepted: dict[str, str] = {}
        self.modes: list[str] = []

    async def send(self, creds: Any, message: Any) -> SendResult:
        mode = self.modes.pop(0) if self.modes else "ok"
        self.sent.append(message)
        if mode == "transient":
            raise TransientSendError("TooManyRequestsException")
        if mode == "permanent":
            raise PermanentSendError("MessageRejected")
        if mode == "ambiguous":
            raise AmbiguousSendError("ReadTimeoutError")
        provider_id = self.accepted.setdefault(message.idempotency_key, f"0100-{uuid.uuid4().hex}")
        if mode == "ambiguous_accepted":  # the provider took it, but the response was lost
            raise AmbiguousSendError("ReadTimeoutError")
        return SendResult(provider_message_id=provider_id)

    async def lookup(self, creds: Any, key: str) -> str | None:
        return self.accepted.get(key)

    async def create_domain_identity(self, creds: Any, domain: str) -> list[str]:
        return fake_dkim_tokens(domain)

    async def domain_status(self, creds: Any, domain: str) -> dict[str, str]:
        return {"verified_for_sending": "true", "dkim_status": "SUCCESS", "identity_type": "DOMAIN"}


class FakeLimiter:
    def __init__(self) -> None:
        self.waits: dict[str, float] = {}
        self.keys: list[str] = []

    async def take(self, key: str, *, rate_per_second: float, burst: int) -> float:
        self.keys.append(key)
        return next((w for prefix, w in self.waits.items() if key.startswith(prefix)), 0.0)


@dataclass
class Remote:
    """State behind the respx fakes of tenant-service and crm-service."""

    comm: dict[str, Any] = field(default_factory=comm_settings)
    audience: list[uuid.UUID] = field(default_factory=list)
    contacts: dict[str, dict[str, Any]] = field(default_factory=dict)  # contact id -> batch item
    suppressed: dict[str, list[str]] = field(default_factory=dict)  # normalized address -> reasons
    suppression_posts: list[dict[str, Any]] = field(default_factory=list)
    crm_down: bool = False
    segment_missing: bool = False
    confirmations: list[str] = field(default_factory=list)

    def add_contacts(self, n: int, *, domain: str = "example.test") -> list[uuid.UUID]:
        ids = []
        for i in range(n):
            cid = uuid.uuid4()
            ids.append(cid)
            self.audience.append(cid)
            self.contacts[str(cid)] = {
                "contact_id": str(cid),
                "allowed": True,
                "reasons": [],
                "email": f"person{i}-{cid.hex[:6]}@{domain}",
                "first_name": f"P{i}",
                "last_name": "Test",
            }
        return ids


def _install(mock: respx.MockRouter, remote: Remote) -> None:
    def settings(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=remote.comm)

    def resolve(request: httpx.Request) -> httpx.Response:
        if remote.crm_down:
            return httpx.Response(503, json={"code": "unavailable"})
        if remote.segment_missing:
            return httpx.Response(404, json={"code": "not_found"})
        body = json.loads(request.content)
        start = int(body["cursor"]) if body.get("cursor") else 0
        page = remote.audience[start : start + body["limit"]]
        end = start + len(page)
        nxt = str(end) if end < len(remote.audience) else None
        return httpx.Response(200, json={"items": [{"contact_id": str(i)} for i in page], "next_cursor": nxt})

    def batch(request: httpx.Request) -> httpx.Response:
        if remote.crm_down:
            return httpx.Response(503, json={"code": "unavailable"})
        ids = json.loads(request.content)["contact_ids"]
        return httpx.Response(200, json={"items": [remote.contacts[i] for i in ids if i in remote.contacts]})

    def check(request: httpx.Request) -> httpx.Response:
        if remote.crm_down:
            return httpx.Response(503, json={"code": "unavailable"})
        reasons = remote.suppressed.get(request.url.params["value"].strip().lower(), [])
        return httpx.Response(200, json={"suppressed": bool(reasons), "reasons": reasons})

    def suppress(request: httpx.Request) -> httpx.Response:
        if remote.crm_down:
            return httpx.Response(503, json={"code": "unavailable"})
        body = json.loads(request.content)
        remote.suppression_posts.append({**body, "idempotency_key": request.headers.get("Idempotency-Key")})
        remote.suppressed.setdefault(body["value"].lower(), []).append(body["reason"])
        return httpx.Response(201, json={"suppression_id": str(uuid.uuid4())})

    def confirm(request: httpx.Request) -> httpx.Response:
        remote.confirmations.append(str(request.url))
        return httpx.Response(200, json={"status": "confirmed"})

    mock.get(url__regex=r"^http://tenant-service:8000/internal/v1/tenants/[^/]+/settings/communication$").mock(
        side_effect=settings
    )
    mock.post(url__regex=r"^http://crm-service:8000/internal/v1/segments/[^/]+/resolve$").mock(side_effect=resolve)
    mock.post("http://crm-service:8000/internal/v1/contacts/batch").mock(side_effect=batch)
    mock.get(url__regex=r"^http://crm-service:8000/internal/v1/suppressions/check").mock(side_effect=check)
    mock.post("http://crm-service:8000/internal/v1/suppressions").mock(side_effect=suppress)
    mock.get("http://fake-providers:8000/sns/cert.pem").mock(
        return_value=httpx.Response(200, content=certificate_pem())
    )
    mock.get(url__regex=r"^http://fake-providers:8000/ses/_confirm").mock(side_effect=confirm)


def make_settings(app_url: str, **overrides: Any) -> Settings:
    return Settings(
        env="test",
        provider_mode="fake",
        database_url=app_url,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=TEST_AUDIENCE,
        redis_url="redis://127.0.0.1:1/0",
        outbox_relay_enabled=False,
        event_consumers_enabled=False,
        periodic_jobs_enabled=False,
        public_base_url="https://app.test",
        threshold_min_sample=4,
        **overrides,
    )


class JobDeps:
    """Stands in for `WorkerDeps` / `AppDeps` when a test runs a coroutine directly."""

    def __init__(self, harness: Harness, database: Database) -> None:
        self.db = database
        self.redis = None
        self.settings = harness.settings
        self.provider = harness.provider
        self.limiter = harness.limiter
        self.credentials = CredentialResolver(harness.settings, None, None)
        self._harness = harness

    def client(self, name: str) -> ServiceClient:
        tokens = self._harness.tokens

        async def token() -> str:
            return tokens.service("svc-email-service")

        return ServiceClient(name, f"http://{name}:8000", token)

    async def enqueue(self, name: str, queue: str, ctx: JobContext, *, countdown: float | None = None,
                      **ids: str) -> None:  # fmt: skip
        self._harness.enqueued.append((name, queue, ctx, {**ids, "countdown": countdown}))


@dataclass
class Harness:
    client: TestClient
    app: Any
    tokens: TokenFactory
    authz: StaticAuthzResolver
    remote: Remote
    settings: Settings
    provider: FakeProvider
    limiter: FakeLimiter
    app_url: str
    enqueued: list[tuple[str, str, JobContext, dict[str, Any]]]

    def user(self, tenant_id: uuid.UUID, perms: set[str] | None = None, *, mfa: bool = False) -> dict[str, str]:
        sub = str(uuid.uuid4())
        self.authz.grant(sub, tenant_id, {MANAGE} if perms is None else perms)
        return auth(self.tokens.user(sub, f"u-{sub[:8]}@example.test", mfa=mfa), tenant_id)

    def service(self, client_id: str, tenant_id: uuid.UUID | None) -> dict[str, str]:
        return auth(self.tokens.service(client_id), tenant_id)

    @staticmethod
    def ik(key: str | None = None) -> dict[str, str]:
        return {"Idempotency-Key": key or str(uuid.uuid4())}

    def run(self, fn: Any) -> Any:
        """Run `await fn(deps)` in one event loop with its own connection pool."""

        async def runner() -> Any:
            database = Database(self.app_url, pool_size=2, max_overflow=0)
            try:
                return await fn(JobDeps(self, database))
            finally:
                await database.dispose()

        return asyncio.run(runner())

    def sql(self, query: str, params: dict[str, Any] | None = None, *, tenant_id: uuid.UUID | None = None) -> list:
        async def go(deps: JobDeps) -> list:
            ctx = deps.db.tenant_session(tenant_id) if tenant_id else deps.db.global_session()
            async with ctx as s:
                result = await s.execute(text(query), params or {})
                return [dict(r) for r in result.mappings().all()] if result.returns_rows else []

        return self.run(go)

    def events(self, event_type: str) -> list[dict[str, Any]]:
        rows = self.sql("SELECT envelope FROM outbox WHERE event_type = :t ORDER BY id", {"t": event_type})
        return [r["envelope"] for r in rows]

    def send_pending(self, tenant_id: uuid.UUID) -> list[Any]:
        """Run the send coroutine for every queued job recorded so far (like a worker would)."""
        from email_service.sending import send_message

        jobs, self.enqueued[:] = list(self.enqueued), []
        outcomes = []
        for name, _queue, ctx, ids in jobs:
            if name in ("emails.campaign_send", "emails.send_message") and ctx.tenant_id == tenant_id:
                outcomes.append(self.run(lambda d, c=ctx, i=ids: send_message(d, c, uuid.UUID(i["message_id"]))))
            else:
                self.enqueued.append((name, _queue, ctx, ids))
        return outcomes

    # ----------------------------------------------------------------------------------------- builders
    def domain(self, tenant_id: uuid.UUID, name: str | None = None, *, verify: bool = True) -> dict[str, Any]:
        headers = self.user(tenant_id)
        r = self.client.post("/api/v1/email/domains", headers=headers,
                             json={"domain": name or f"d{uuid.uuid4().hex[:8]}.test"})  # fmt: skip
        assert r.status_code == 201, r.text
        if verify:
            r = self.client.post(f"/api/v1/email/domains/{r.json()['id']}/verify", headers=headers)
            assert r.status_code == 200, r.text
        return dict(r.json())

    def sender(self, tenant_id: uuid.UUID, domain: dict[str, Any] | None = None) -> dict[str, Any]:
        domain = domain or self.domain(tenant_id)
        r = self.client.post("/api/v1/email/senders", headers=self.user(tenant_id),
                             json={"domain_id": domain["id"], "from_name": "Acme News", "local_part": "news"})  # fmt: skip
        assert r.status_code == 201, r.text
        return dict(r.json())

    def template(self, tenant_id: uuid.UUID, *, category: str = "campaign", html: str | None = None) -> dict[str, Any]:
        r = self.client.post(
            "/api/v1/email/templates",
            headers=self.user(tenant_id),
            json={
                "name": f"t-{uuid.uuid4().hex[:6]}",
                "category": category,
                "subject": "Hello {{first_name}}",
                "html": html or '<p>Hi {{first_name}}!</p><p><a href="https://www.example.com/x">More</a></p>',
            },
        )
        assert r.status_code == 201, r.text
        return dict(r.json())

    def campaign(self, tenant_id: uuid.UUID, *, sender: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
        sender = sender or self.sender(tenant_id)
        template = self.template(tenant_id)
        body = {"name": "Spring", "sender_id": sender["id"], "template_id": template["id"],
                "segment_id": str(uuid.uuid4()), **extra}  # fmt: skip
        r = self.client.post("/api/v1/email/campaigns", headers=self.user(tenant_id), json=body)
        assert r.status_code == 201, r.text
        return dict(r.json())

    def launch(self, tenant_id: uuid.UUID, campaign_id: str, key: str | None = None) -> httpx.Response:
        return self.client.post(f"/api/v1/email/campaigns/{campaign_id}/launch",
                                headers={**self.user(tenant_id), **self.ik(key)})  # fmt: skip

    def internal_send(self, tenant_id: uuid.UUID, body: dict[str, Any], key: str | None = None) -> httpx.Response:
        return self.client.post("/internal/v1/send", json=body,
                                headers={**self.service(INBOX_CLIENT, tenant_id), **self.ik(key)})  # fmt: skip

    def message(self, message_id: str | uuid.UUID, tenant_id: uuid.UUID) -> dict[str, Any]:
        rows = self.sql("SELECT * FROM email_messages WHERE id = :i", {"i": str(message_id)}, tenant_id=tenant_id)
        return rows[0]

    def sns(self, event: dict[str, Any], *, topic: str = FAKE_SES_EVENTS_TOPIC) -> dict[str, str]:
        return sign_sns(topic, json.dumps(event))

    def post_sns(self, envelope: dict[str, Any]) -> httpx.Response:
        return self.client.post("/webhooks/ses/events", content=json.dumps(envelope),
                                headers={"Content-Type": "text/plain; charset=UTF-8"})  # fmt: skip


def ses_event(kind: str, provider_message_id: str, to: str, *, tag: str | None = None, **extra: Any) -> dict:
    mail = {"messageId": provider_message_id, "destination": [to], "source": "news@x.test",
            "tags": {"nexus_message_id": [tag]} if tag else {}}  # fmt: skip
    event: dict[str, Any] = {"eventType": kind, "mail": mail}
    if kind == "Bounce":
        event["bounce"] = {"bounceType": extra.get("bounce_type", "Permanent"),
                           "bouncedRecipients": [{"emailAddress": to}]}  # fmt: skip
    if kind == "Complaint":
        event["complaint"] = {"complainedRecipients": [{"emailAddress": to}], "complaintFeedbackType": "abuse"}
    return event


@pytest.fixture
def harness(sdb, tokens):
    from email_service.main import create_app

    settings = make_settings(sdb.app_url)
    authz = StaticAuthzResolver()
    recorded: list[tuple[str, str, JobContext, dict[str, Any]]] = []

    async def enqueue(name: str, queue: str, ctx: JobContext, *, countdown: float | None = None, **ids: str) -> None:
        recorded.append((name, queue, ctx, {**ids, "countdown": countdown}))

    provider = FakeProvider()
    app = create_app(settings, authz_resolver=authz, enqueue=enqueue, provider=provider, dns=FakeDnsVerifier())
    app.state.token_verifier = tokens.verifier()
    remote = Remote()

    async def svc_token() -> str:
        return tokens.service("svc-email-service")

    with respx.mock(assert_all_called=False) as mock:
        _install(mock, remote)
        with TestClient(app) as client:
            app.state.redis = None
            app.state.service_token = svc_token
            yield Harness(client, app, tokens, authz, remote, settings, provider, FakeLimiter(), sdb.app_url, recorded)


@pytest.fixture
def tenants():
    return uuid.uuid4(), uuid.uuid4()


def table_rows(harness: Harness, table: Any, tenant_id: uuid.UUID, *where: Any) -> list[dict[str, Any]]:
    async def go(deps: JobDeps) -> list[dict[str, Any]]:
        async with deps.db.tenant_session(tenant_id) as s:
            return [dict(r) for r in (await s.execute(select(table).where(*where))).mappings().all()]

    return harness.run(go)

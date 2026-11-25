"""Integration fixtures: one real PostgreSQL per session (restricted call_app role + forced RLS), locally signed
JWTs, static authorization, respx fakes for tenant/crm/campaign services and a scripted telephony adapter."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from call_service.providers.twilio import NotSent
from call_service.settings import Settings
from nexus_core.celery_app import JobContext
from nexus_core.clients import ServiceClient
from nexus_core.config import ProviderMode
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
from nexus_core.webhooks import twilio_signature
from nexus_providers.telephony import AmbiguousProviderResult, ProviderCall, ProviderRejected

SERVICE_DIR = Path(__file__).resolve().parents[1]
SECRET = "test-fake-provider-secret"
WEBHOOK_BASE = "http://call.test"
ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


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
    return provision_and_migrate(pg, "call", SERVICE_DIR)


@pytest.fixture(scope="session")
def tokens():
    return TokenFactory()


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
        "provider_mode": "fake",
        "fake_provider_secret": SECRET,
        "webhook_base_url": WEBHOOK_BASE,
        "media_stream_url": "wss://voice.test/media/twilio",
        "stream_token_secret": "test-stream-secret",
    }
    base.update(overrides)
    return Settings(**base)


# --------------------------------------------------------------------------------------- scripted telephony
@dataclass
class ScriptedTelephony:
    """In-memory provider. `mode` per destination number: ok | ambiguous_created | ambiguous_absent | rejected |
    not_sent. Records every create so tests can prove a call row never produced two provider calls."""

    modes: dict[str, str] = field(default_factory=dict)
    calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_token: dict[str, str] = field(default_factory=dict)
    creates: list[dict[str, Any]] = field(default_factory=list)
    hangups: list[tuple[str, str]] = field(default_factory=list)
    transfers: list[tuple[str, str]] = field(default_factory=list)
    dials: list[dict[str, Any]] = field(default_factory=list)
    lookup_fails: bool = False
    name: str = "scripted"

    def _new(self, req: Any) -> ProviderCall:
        sid = "CA" + uuid.uuid4().hex
        self.calls[sid] = {"sid": sid, "status": "queued", "token": req.idempotency_key, "to": req.to}
        self.by_token[req.idempotency_key] = sid
        return ProviderCall(provider_call_id=sid, status="queued")

    async def create_call(self, creds: Any, req: Any) -> ProviderCall:
        self.creates.append({"token": req.idempotency_key, "to": req.to, "from": req.from_, "creds": creds})
        if req.idempotency_key in self.by_token:
            return ProviderCall(provider_call_id=self.by_token[req.idempotency_key], status="queued")
        mode = self.modes.get(req.to, "ok")
        if mode == "ambiguous_created":
            self._new(req)
            raise AmbiguousProviderResult("timeout")
        if mode == "ambiguous_absent":
            self.modes[req.to] = "ok"  # the provider recovers for the next attempt
            raise AmbiguousProviderResult("http_503")
        if mode == "rejected":
            raise ProviderRejected("twilio_21211")
        if mode == "not_sent":
            raise NotSent("ConnectError")
        return self._new(req)

    async def get_call(self, creds: Any, provider_call_id: str) -> ProviderCall:
        c = self.calls[provider_call_id]
        return ProviderCall(provider_call_id=provider_call_id, status=c["status"])

    async def find_call(self, creds: Any, *, idempotency_key: str, to: str, from_: str, created_after: Any) -> Any:
        if self.lookup_fails:
            raise AmbiguousProviderResult("lookup_failed")
        sid = self.by_token.get(idempotency_key)
        return ProviderCall(provider_call_id=sid, status="ringing") if sid else None

    async def hangup(self, creds: Any, provider_call_id: str, *, status: str = "completed") -> None:
        self.hangups.append((provider_call_id, status))

    async def transfer_to_conference(self, creds: Any, provider_call_id: str, conference_name: str) -> None:
        self.transfers.append((provider_call_id, conference_name))

    async def dial_into_conference(self, creds: Any, **kw: Any) -> ProviderCall:
        self.dials.append(kw)
        return ProviderCall(provider_call_id="CA" + uuid.uuid4().hex, status="queued")

    def live_provider_calls_for(self, call_id: uuid.UUID) -> int:
        return sum(1 for c in self.calls.values() if c["token"].startswith(f"{call_id}:"))


# --------------------------------------------------------------------------------------- remote services
@dataclass
class Remote:
    comm: dict[str, dict[str, Any]] = field(default_factory=dict)  # tenant -> communication section
    profile: dict[str, Any] = field(default_factory=lambda: {"timezone": "UTC", "business_hours": None})
    contacts: dict[str, dict[str, Any]] = field(default_factory=dict)  # contact id -> contactability payload
    campaigns: dict[str, dict[str, Any]] = field(default_factory=dict)
    lookup: dict[str, str] = field(default_factory=dict)  # phone -> contact id
    crm_down: bool = False

    def communication(self, tenant: str) -> dict[str, Any]:
        return self.comm.setdefault(
            tenant,
            {
                "calls_enabled": True,
                "effective_calls_enabled": True,
                "calling_window": {"start": "00:00", "end": "23:59", "days": ALL_DAYS},
                "max_concurrent_calls": 5,
                "max_daily_calls": 100,
                "allowed_call_countries": ["US"],
                "ai_disclosure_enabled": True,
                "recording_enabled": True,
                "recording_consent_prompt": True,
            },
        )

    def add_contact(
        self, *, allowed: bool = True, phone: str = "+12125550123", tz: str = "America/New_York", **kw: Any
    ) -> uuid.UUID:
        cid = uuid.uuid4()
        self.contacts[str(cid)] = {
            "contact_id": str(cid),
            "allowed": allowed,
            "reasons": [] if allowed else kw.get("reasons", ["suppressed"]),
            "phone_e164": phone if allowed else None,
            "timezone": tz,
            "country": "US",
        }
        return cid


def install(mock: respx.MockRouter, remote: Remote) -> None:
    def settings(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")
        tenant, section = parts[4], parts[6]
        if section == "communication":
            return httpx.Response(200, json=remote.communication(tenant))
        return httpx.Response(200, json=remote.profile)

    def contactability(request: httpx.Request) -> httpx.Response:
        if remote.crm_down:
            return httpx.Response(503, json={"code": "unavailable"})
        cid = request.url.path.split("/")[4]
        body = remote.contacts.get(cid)
        if body is None:
            return httpx.Response(404, json={"code": "not_found"})
        return httpx.Response(200, json=body)

    def lookup(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"contact_id": remote.lookup.get(request.url.params.get("phone", ""))})

    def dispatch_state(request: httpx.Request) -> httpx.Response:
        cid = request.url.path.split("/")[4]
        state = remote.campaigns.get(cid, {"state": "running", "dispatch_allowed": True})
        return httpx.Response(200, json={"campaign_id": cid, **state})

    mock.get(url__regex=r"^http://tenant-service:8000/internal/v1/tenants/[^/]+/settings/[a-z]+$").mock(
        side_effect=settings
    )
    mock.get(url__regex=r"^http://crm-service:8000/internal/v1/contacts/[^/]+/contactability").mock(
        side_effect=contactability
    )
    mock.get(url__regex=r"^http://crm-service:8000/internal/v1/contacts/lookup").mock(side_effect=lookup)
    mock.get(url__regex=r"^http://campaign-service:8000/internal/v1/campaigns/[^/]+/dispatch-state$").mock(
        side_effect=dispatch_state
    )


# --------------------------------------------------------------------------------------- deps for task bodies
class TestDeps:
    """`Deps` for calling task coroutines directly in their own event loop (own DB pool, no Redis)."""

    __test__ = False

    def __init__(self, settings: Settings, telephony: ScriptedTelephony, tokens: TokenFactory, credentials: Any = None):
        self.settings = settings
        self.db = Database(settings.database_url.get_secret_value(), pool_size=2, max_overflow=0)
        self.redis = None
        self.telephony = telephony
        self.credentials = credentials or CredentialResolver(settings, None, None)
        self.enqueued: list[tuple[str, dict[str, str]]] = []
        self._tokens = tokens

    def client(self, service: str) -> ServiceClient:
        async def token() -> str:
            return self._tokens.service("svc-call-service")

        return ServiceClient(service, self.settings.service_url(service), token)

    async def enqueue(self, name: str, queue: str, ctx: JobContext, **ids: str) -> None:
        self.enqueued.append((name, ids))


class Harness:
    def __init__(
        self, client: TestClient, app: Any, remote: Remote, telephony: ScriptedTelephony, tokens: TokenFactory, sdb: Any
    ):
        self.client, self.app, self.remote, self.telephony, self.tokens, self.sdb = (
            client,
            app,
            remote,
            telephony,
            tokens,
            sdb,
        )
        self.authz: StaticAuthzResolver = app.state.authz_resolver
        self.enqueued: list[tuple[str, dict[str, str]]] = app.state.enqueued

    # ---- identities
    def user(self, tenant: uuid.UUID, perms: set[str]) -> str:
        uid = uuid.uuid4()
        self.authz.grant(uid, tenant, perms)
        return self.tokens.user(uid, f"u-{uid.hex[:6]}@example.test")

    def service(self, name: str = "svc-campaign-service") -> str:
        return self.tokens.service(name)

    # ---- helpers
    def create_call(
        self, tenant: uuid.UUID, contact_id: uuid.UUID, *, key: str | None = None, **body: Any
    ) -> httpx.Response:
        payload = {"contact_id": str(contact_id), "agent_id": str(body.pop("agent_id", uuid.uuid4())), **body}
        return self.client.post(
            "/internal/v1/calls",
            headers=auth(self.service(), tenant, **{"Idempotency-Key": key or uuid.uuid4().hex}),
            json=payload,
        )

    def run(self, coro_fn: Any, *args: Any, credentials: Any = None, **kwargs: Any) -> Any:
        """Run a task coroutine `coro_fn(deps, *args)` in a fresh loop with its own DB pool."""

        async def go() -> Any:
            deps = TestDeps(self.app.state.settings, self.telephony, self.tokens, credentials)
            try:
                result = await coro_fn(deps, *args, **kwargs)
            finally:
                await deps.db.dispose()
            self.enqueued.extend(deps.enqueued)
            return result

        return asyncio.run(go())

    def ctx(self, tenant: uuid.UUID) -> JobContext:
        return JobContext(tenant_id=tenant, correlation_id=uuid.uuid4())

    def sign(self, url: str, params: dict[str, str], secret: str = SECRET) -> dict[str, str]:
        return {"X-Twilio-Signature": twilio_signature(secret, url, params)}

    def post_webhook(
        self, path_qs: str, params: dict[str, str], *, secret: str = SECRET, signature: str | None = None
    ) -> httpx.Response:
        url = f"{WEBHOOK_BASE}{path_qs}"
        headers = {"X-Twilio-Signature": signature} if signature is not None else self.sign(url, params, secret)
        return self.client.post(path_qs, data=params, headers=headers)

    def sql(self, query: str, params: dict[str, Any] | None = None, *, tenant: uuid.UUID | None = None) -> list[Any]:
        from sqlalchemy import text

        async def go() -> list[Any]:
            db = Database(self.sdb.app_url, pool_size=1, max_overflow=0)
            try:
                ctx = db.tenant_session(tenant) if tenant else db.global_session()
                async with ctx as s:
                    return list((await s.execute(text(query), params or {})).mappings().all())
            finally:
                await db.dispose()

        return asyncio.run(go())

    def events(self, event_type: str, call_id: uuid.UUID | str) -> list[dict[str, Any]]:
        return outbox_events(self, event_type, call_id)

    def add_number(
        self, tenant: uuid.UUID, e164: str, *, agent_id: uuid.UUID | None = None, **kw: Any
    ) -> dict[str, Any]:
        token = self.user(tenant, {"calls:manage"})
        r = self.client.post(
            "/api/v1/inbound-numbers",
            headers=auth(token, tenant),
            json={"e164": e164, "agent_id": str(agent_id or uuid.uuid4()), **kw},
        )
        assert r.status_code == 201, r.text
        return r.json()


@pytest.fixture
def harness(sdb, tokens):
    from call_service.main import create_app

    settings = make_settings(sdb)
    enqueued: list[tuple[str, dict[str, str]]] = []

    async def capture(name: str, queue: str, ctx: JobContext, **ids: str) -> None:
        enqueued.append((name, ids))

    app = create_app(settings, authz_resolver=StaticAuthzResolver(), enqueue=capture)
    app.state.token_verifier = tokens.verifier()
    app.state.enqueued = enqueued
    telephony = ScriptedTelephony()
    remote = Remote()

    async def svc_token() -> str:
        return tokens.service("svc-call-service")

    with respx.mock(assert_all_called=False) as mock:
        install(mock, remote)
        with TestClient(app) as client:
            app.state.service_token = svc_token
            app.state.redis = None
            app.state.telephony = telephony
            assert app.state.settings.provider_mode is ProviderMode.FAKE
            yield Harness(client, app, remote, telephony, tokens, sdb)


def outbox_events(h: Harness, event_type: str, call_id: uuid.UUID | str) -> list[dict[str, Any]]:
    rows = h.sql(
        "SELECT envelope FROM outbox WHERE event_type = :t AND envelope->'payload'->>'call_id' = :c ORDER BY id",
        {"t": event_type, "c": str(call_id)},
    )
    return [r["envelope"] for r in rows]


def now_utc() -> datetime:
    return datetime.now(UTC)


def dumps(value: Any) -> str:
    return json.dumps(value)

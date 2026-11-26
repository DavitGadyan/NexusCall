"""Booking fixtures: one real PostgreSQL per session (restricted booking_app role + forced RLS), locally signed
JWTs, static authorization, and the Google Calendar simulator mounted in-process (httpx ASGITransport), so the
production adapter code runs end to end without any network."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from booking_service.providers.calendar import GoogleCalendarProvider, GoogleTokenSource
from booking_service.settings import Settings
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

SERVICE_DIR = Path(__file__).resolve().parents[1]
SECRET = "test-fake-provider-secret"
os.environ["FAKE_PROVIDER_SECRET"] = SECRET  # read by the in-process simulator
SIM_BASE = "http://fake-providers.test"

READ = {"bookings:read"}
MANAGE = {"bookings:read", "bookings:manage"}


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
    return provision_and_migrate(pg, "booking", SERVICE_DIR)


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
        "fake_providers_url": SIM_BASE,
    }
    base.update(overrides)
    return Settings(**base)


class FlakyTransport(httpx.AsyncBaseTransport):
    """Wraps the simulator. `ambiguous_inserts` forwards the request (so the event IS created) and then raises
    a timeout, exactly the "did it happen?" case that must be reconciled rather than retried blindly."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.ambiguous_inserts = 0
        self.down = False
        self.requests: list[tuple[str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if self.down:
            raise httpx.ConnectError("simulated outage", request=request)
        response = await self.inner.handle_async_request(request)
        if request.method == "POST" and request.url.path.endswith("/events") and self.ambiguous_inserts > 0:
            self.ambiguous_inserts -= 1
            await response.aread()
            raise httpx.ReadTimeout("simulated timeout after the write", request=request)
        return response


def simulator_app() -> FastAPI:
    from fake_providers.routers import google_calendar

    google_calendar.STATE.reset()
    sim = FastAPI()
    sim.include_router(google_calendar.router)
    return sim


class Harness:
    def __init__(self, client: TestClient, app: FastAPI, tokens: TokenFactory, authz: StaticAuthzResolver,
                 transport: FlakyTransport, sdb: Any) -> None:  # fmt: skip
        self.client, self.app, self.tokens, self.authz, self.transport, self.sdb = (
            client, app, tokens, authz, transport, sdb,
        )  # fmt: skip

    # ---------------------------------------------------------------- identities
    def user(self, tenant_id: uuid.UUID, permissions: set[str]) -> str:
        uid = uuid.uuid4()
        self.authz.grant(uid, tenant_id, permissions)
        return self.tokens.user(uid, f"u{uid.hex[:6]}@example.test")

    def service(self, client_id: str = "svc-tool-execution") -> str:
        return self.tokens.service(client_id)

    def internal(self, tenant_id: uuid.UUID, key: str | None = None, client_id: str = "svc-tool-execution",
                 actor: str = "user:00000000-0000-4000-8000-000000000104") -> dict[str, str]:  # fmt: skip
        headers = auth(self.service(client_id), tenant_id, **{"X-Actor": actor})
        if key:
            headers["Idempotency-Key"] = key
        return headers

    # ---------------------------------------------------------------- data
    def resource(self, tenant_id: uuid.UUID, *, calendar: bool = False, tz: str = "UTC",
                 slot_minutes: int = 30, buffer_after: int = 0, days: range = range(7)) -> str:  # fmt: skip
        admin = self.user(tenant_id, MANAGE)
        body: dict[str, Any] = {"name": f"Res {uuid.uuid4().hex[:6]}", "timezone": tz}
        if calendar:
            body |= {"calendar_provider": "google_calendar", "calendar_id": f"cal-{uuid.uuid4().hex[:8]}"}
        r = self.client.post("/api/v1/bookings/resources", headers=auth(admin, tenant_id), json=body)
        assert r.status_code == 201, r.text
        res = r.json()
        rules = [
            {"weekday": d, "start_time": "08:00:00", "end_time": "18:00:00", "slot_minutes": slot_minutes,
             "buffer_after_minutes": buffer_after, "min_notice_minutes": 0}
            for d in days
        ]  # fmt: skip
        r = self.client.put(
            f"/api/v1/bookings/resources/{res['id']}/rules",
            headers=auth(admin, tenant_id),
            json={"rules": rules, "expected_version": res["version"]},
        )
        assert r.status_code == 200, r.text
        return str(res["id"])

    def run(self, coro: Any) -> Any:
        return self.client.portal.call(lambda: coro)  # type: ignore[union-attr]

    def sql(self, query: str, params: dict[str, Any] | None = None, *, tenant_id: uuid.UUID | None = None) -> Any:
        async def go() -> Any:
            database: Database = self.app.state.db
            ctx = database.tenant_session(tenant_id) if tenant_id else database.global_session()
            async with ctx as s:
                result = await s.execute(text(query), params or {})
                return result.mappings().all() if result.returns_rows else None

        return self.run(go())

    def outbox(self, event_type: str) -> list[dict[str, Any]]:
        from booking_service.tables import outbox

        async def go() -> list[dict[str, Any]]:
            async with self.app.state.db.global_session() as s:
                rows = (await s.execute(select(outbox.c.envelope).where(outbox.c.event_type == event_type))).all()
            return [r.envelope for r in rows]

        return self.run(go())


def slot_start(days_ahead: int = 3, hour: int = 10, minute: int = 0) -> datetime:
    day = (datetime.now(UTC) + timedelta(days=days_ahead)).date()
    return datetime.combine(day, time(hour, minute), tzinfo=UTC)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@pytest.fixture
def harness(sdb, tokens) -> Iterator[Harness]:
    from booking_service.main import create_app

    settings = make_settings(sdb)
    authz = StaticAuthzResolver()
    transport = FlakyTransport(httpx.ASGITransport(app=simulator_app()))
    resolver = CredentialResolver(settings, None, None)  # fake mode: no Vault, development secret only
    calendar = GoogleCalendarProvider(f"{SIM_BASE}/google_calendar/calendar/v3", transport=transport)
    token_source = GoogleTokenSource(resolver, f"{SIM_BASE}/google_calendar/token", transport=transport)
    app = create_app(settings, authz_resolver=authz, calendar=calendar, calendar_tokens=token_source,
                     credentials=resolver)  # fmt: skip
    app.state.token_verifier = tokens.verifier()
    with TestClient(app) as client:
        app.state.redis = None
        yield Harness(client, app, tokens, authz, transport, sdb)


@pytest.fixture
def tenant() -> uuid.UUID:
    return uuid.uuid4()


def run_async(coro: Any) -> Any:
    return asyncio.run(coro)

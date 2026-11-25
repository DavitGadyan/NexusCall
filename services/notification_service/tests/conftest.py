"""Integration fixtures: one PostgreSQL container per session (restricted app role, forced RLS), locally
signed JWTs, static authz, a recording email provider and a small in-memory Redis stand-in."""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nexus_core.testing import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    StaticAuthzResolver,
    TokenFactory,
    provision_and_migrate,
    start_postgres,
)
from nexus_providers.email import EmailCredentials, OutboundEmail, SendResult
from notification_service.providers.email import StaticCredentials
from notification_service.settings import Settings

SERVICE_DIR = Path(__file__).resolve().parents[1]
PUBLIC = "https://app.nexus.test"


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
    return provision_and_migrate(pg, "notify", SERVICE_DIR)


@pytest.fixture(scope="session")
def tokens():
    return TokenFactory()


class RecordingProvider:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[OutboundEmail] = []
        self.fail: Exception | None = None

    async def send(self, creds: EmailCredentials, message: OutboundEmail) -> SendResult:
        if self.fail is not None:
            raise self.fail
        self.sent.append(message)
        return SendResult(provider_message_id=f"msg-{len(self.sent)}")

    async def domain_status(self, creds: EmailCredentials, domain: str) -> dict[str, str]:
        return {"verified_for_sending": "true"}


class FakePubSub:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis, self.channels, self.queue, self.closed = redis, set(), deque(), False

    async def subscribe(self, *channels: str) -> None:
        self.channels |= set(channels)
        self.redis.subscribers.append(self)
        for ch in channels:
            for data in self.redis.backlog.get(ch, []):
                self.queue.append({"type": "message", "channel": ch.encode(), "data": data.encode()})

    async def get_message(  # mirrors redis.asyncio.client.PubSub.get_message
        self,
        ignore_subscribe_messages: bool = True,
        timeout: float = 0.0,  # noqa: ASYNC109
    ) -> Any:
        if self.queue:
            return self.queue.popleft()
        await asyncio.sleep(min(timeout, 0.05))
        return None

    async def unsubscribe(self, *channels: str) -> None:
        self.closed = True

    async def aclose(self) -> None:
        self.closed = True


class FakeRedis:
    """Only what notification-service uses: publish/pubsub, INCR/EXPIRE windows and slot scripts."""

    def __init__(self) -> None:
        self.backlog: dict[str, list[str]] = {}
        self.published: list[tuple[str, str]] = []
        self.subscribers: list[FakePubSub] = []
        self.counters: dict[str, int] = {}
        self.slots_released = 0

    async def publish(self, channel: str, data: str) -> int:
        self.published.append((channel, data))
        for sub in self.subscribers:
            if channel in sub.channels and not sub.closed:
                sub.queue.append({"type": "message", "channel": channel.encode(), "data": data.encode()})
        return 1

    def pubsub(self) -> FakePubSub:
        return FakePubSub(self)

    async def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        return 1  # acquire_slot always succeeds

    async def zrem(self, key: str, member: str) -> int:
        self.slots_released += 1
        return 1


class Harness:
    def __init__(self, client: TestClient, app: Any, authz: StaticAuthzResolver, tokens: TokenFactory, provider):
        self.client, self.app, self.authz, self.tokens, self.provider = client, app, authz, tokens, provider

    def member(self, tenant_id: uuid.UUID, perms: set[str]) -> tuple[uuid.UUID, str]:
        uid = uuid.uuid4()
        self.authz.grant(uid, tenant_id, perms)
        return uid, self.tokens.user(uid, f"u-{uid.hex[:6]}@x.test")

    def service(self, client_id: str = "svc-tenant-service") -> str:
        return self.tokens.service(client_id)


def make_settings(sdb, **over: Any) -> Settings:
    values: dict[str, Any] = {
        "env": "test",
        "database_url": sdb.app_url,
        "oidc_issuer": TEST_ISSUER,
        "oidc_audience": TEST_AUDIENCE,
        "redis_url": "redis://127.0.0.1:1/0",
        "outbox_relay_enabled": False,
        "event_consumers_enabled": False,
        "periodic_jobs_enabled": False,
        "public_base_url": PUBLIC,
        "sse_heartbeat_seconds": 0.2,
        "sse_max_seconds": 1.0,
        "sse_authz_recheck_seconds": 0.3,
        **over,
    }
    return Settings(**values)


@pytest.fixture
def harness(sdb, tokens):
    from notification_service.main import create_app

    provider = RecordingProvider()
    app = create_app(
        make_settings(sdb),
        email_provider=provider,
        email_credentials=StaticCredentials(EmailCredentials(region="local", access_key_id="x", secret_access_key="y")),
    )
    app.state.token_verifier = tokens.verifier()
    authz = StaticAuthzResolver()
    app.state.authz_resolver = authz
    with TestClient(app) as client:
        app.state.redis = None
        yield Harness(client, app, authz, tokens, provider)


@pytest.fixture
def fake_redis(harness) -> FakeRedis:
    redis = FakeRedis()
    harness.app.state.redis = redis
    return redis


@pytest.fixture(scope="session")
def superuser_url(pg, sdb) -> str:
    """Only for assertions on rows the runtime role must not see (platform-level log rows)."""
    return f"postgresql+asyncpg://{pg.username}:{pg.password}@{sdb.host}:{sdb.port}/notify"


@pytest.fixture
def run_db(sdb):
    """Run `fn(database)` on a fresh pool (asyncpg pools are bound to one event loop)."""
    from nexus_core.db import Database

    def _run(fn: Any, url: str | None = None) -> Any:
        async def go() -> Any:
            database = Database(url or sdb.app_url, pool_size=2, max_overflow=0)
            try:
                return await fn(database)
            finally:
                await database.dispose()

        return asyncio.run(go())

    return _run

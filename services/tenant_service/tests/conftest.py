"""Integration fixtures: real PostgreSQL (restricted app role + forced RLS), in-memory IdP,
mocked notification-service, locally signed JWTs. Requires Docker."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from nexus_core.testing import TEST_AUDIENCE, TEST_ISSUER, TokenFactory, provision_and_migrate, start_postgres
from tenant_service.idp import FakeIdp
from tenant_service.settings import Settings

SERVICE_DIR = Path(__file__).resolve().parents[1]


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
    return provision_and_migrate(pg, "tenant", SERVICE_DIR)


@pytest.fixture(scope="session")
def tokens():
    return TokenFactory()


class Harness:
    def __init__(self, client: TestClient, idp: FakeIdp, tokens: TokenFactory, mails: list[dict]):
        self.client, self.idp, self.tokens, self.mails = client, idp, tokens, mails

    def user(
        self, email: str, *, roles: list[str] | None = None, mfa: bool = False, sub: str | None = None
    ) -> tuple[str, str]:
        existing = next((u for u in self.idp.users.values() if u.email == email), None)
        uid = sub or (existing.id if existing else str(uuid.uuid4()))
        if existing is None:
            self.idp.add_user(uid, email, roles=set(roles or []))
        return uid, self.tokens.user(uid, email, roles=roles, mfa=mfa)

    def last_invite_token(self) -> str:
        link = self.mails[-1]["variables"]["link"]
        return link.split("token=", 1)[1]


@pytest.fixture
def harness(sdb, tokens):
    idp = FakeIdp()
    settings = Settings(
        env="test",
        database_url=sdb.app_url,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=TEST_AUDIENCE,
        redis_url="redis://127.0.0.1:1/0",
        outbox_relay_enabled=False,
        event_consumers_enabled=False,
        notification_service_url="http://notify.test",
        public_base_url="http://app.test",
    )
    from tenant_service.main import create_app

    app = create_app(settings, idp=idp)
    app.state.token_verifier = tokens.verifier()

    async def svc_token() -> str:
        return tokens.service("svc-tenant-service")

    mails: list[dict] = []
    with respx.mock(assert_all_called=False) as mock:

        def capture(request):
            import json

            mails.append(json.loads(request.content))
            return Response(202, json={"status": "sent"})

        mock.post("http://notify.test/internal/v1/notifications/email").mock(side_effect=capture)
        with TestClient(app) as client:
            app.state.service_token = svc_token
            app.state.redis = None  # rate-limit/epoch skipped; covered separately
            yield Harness(client, idp, tokens, mails)

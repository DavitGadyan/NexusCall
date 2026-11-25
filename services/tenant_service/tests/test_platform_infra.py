"""Platform infrastructure operations: metadata-only dead-letter view, audited replay/discard, permissions."""

from __future__ import annotations

import asyncio
import json
import uuid

import aio_pika
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from nexus_core.celery_app import declare_dead_letter_queues
from nexus_core.db import Database
from nexus_core.testing import TEST_AUDIENCE, TEST_ISSUER, auth
from tenant_service.api.platform_infra import _kind, _replay_target, describe_dead_letter
from tenant_service.idp import FakeIdp
from tenant_service.settings import Settings

TENANT = str(uuid.uuid4())


def test_dead_letter_description_never_contains_the_body():
    task_body = json.dumps(
        [
            [],
            {
                "ctx": {"tenant_id": TENANT, "correlation_id": "c-1"},
                "call_id": "abc",
                "note": "free text that must not be shown " * 5,
            },
            {},
        ]
    ).encode()
    headers = {"task": "calls.dispatch", "x-death": [{"reason": "rejected", "count": 2, "queue": "calls.dispatch"}]}
    item = describe_dead_letter("calls.dispatch.dlq", task_body, headers)
    assert (item.kind, item.name, item.tenant_id, item.correlation_id) == ("task", "calls.dispatch", TENANT, "c-1")
    assert item.identifiers == {"call_id": "abc"} and item.death_reason == "rejected" and item.death_count == 2
    assert "free text" not in item.model_dump_json()

    event_body = json.dumps(
        {
            "event_type": "call.completed",
            "event_id": "e-1",
            "tenant_id": TENANT,
            "correlation_id": "c-2",
            "payload": {"call_id": "x", "transcript": "secret words"},
        }
    ).encode()
    event = describe_dead_letter("events.crm_service.dlq", event_body, {})
    assert event.kind == "event" and event.name == "call.completed" and event.identifiers["call_id"] == "x"
    assert "secret words" not in event.model_dump_json()
    assert describe_dead_letter("x.dlq", b"\xff\xfe not json", {}).kind == "unknown"


def test_replay_targets_and_queue_kinds():
    assert _replay_target("calls.dispatch.dlq") == ("nexus.tasks", "calls.dispatch")
    assert _replay_target("events.crm_service.dlq") == ("", "events.crm_service")
    with pytest.raises(Exception, match="not a known dead-letter queue"):
        _replay_target("something-else.dlq")
    assert _kind("calls.dispatch") == "task" and _kind("events.audit_service") == "event"
    assert _kind("calls.dispatch.dlq") == "dead_letter" and _kind("amq.gen-1") == "other"


# ------------------------------------------------------------------------------- with a real broker
pytestmark_integration = [pytest.mark.integration, pytest.mark.security]


@pytest.fixture(scope="module")
def rabbit():
    tc = pytest.importorskip("testcontainers.rabbitmq")
    try:
        container = tc.RabbitMqContainer("rabbitmq:4.0.5-management-alpine").with_exposed_ports(15672)
        container.start()
    except Exception as exc:  # noqa: BLE001 - skip when Docker is unavailable
        pytest.skip(f"Docker unavailable: {exc}")
    yield container
    container.stop()


class _Vault:
    async def read(self, path: str):
        assert path.endswith("platform/tenant-service/rabbitmq-ops")
        return {"username": SecretStr("guest"), "password": SecretStr("guest")}

    async def close(self) -> None:
        return None


@pytest.fixture
def infra(sdb, tokens, rabbit):
    host, port = rabbit.get_container_host_ip(), int(rabbit.get_exposed_port(5672))
    settings = Settings(
        env="test",
        database_url=sdb.app_url,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=TEST_AUDIENCE,
        redis_url="redis://127.0.0.1:1/0",
        outbox_relay_enabled=False,
        event_consumers_enabled=False,
        periodic_jobs_enabled=False,
        rabbitmq_amqp_host=host,
        rabbitmq_amqp_port=port,
        rabbitmq_vhost="/",
        rabbitmq_management_url=f"http://{host}:{rabbit.get_exposed_port(15672)}/api",
    )
    from tenant_service.main import create_app

    app = create_app(settings, idp=FakeIdp())
    app.state.token_verifier = tokens.verifier()
    with TestClient(app) as client:
        app.state.vault = _Vault()
        app.state.redis = None
        yield client, f"amqp://guest:guest@{host}:{port}//"


def _platform_user(sdb, tokens, role: str, *, mfa: bool = True) -> str:
    uid, email = str(uuid.uuid4()), f"{role}-{uuid.uuid4().hex[:6]}@nexus.local"

    async def grant() -> None:
        database = Database(sdb.app_url, pool_size=1)
        async with database.global_session() as s:
            await s.execute(text("INSERT INTO users (id, email) VALUES (:i, :e)"), {"i": uid, "e": email})
            await s.execute(
                text("INSERT INTO platform_role_assignments (user_id, role_key) VALUES (:i, :r)"), {"i": uid, "r": role}
            )
        await database.dispose()

    asyncio.run(grant())
    return tokens.user(uid, email, roles=[role], mfa=mfa)


async def _publish(url: str, queue: str, body: bytes, headers: dict) -> None:
    connection = await aio_pika.connect_robust(url)
    async with connection:
        channel = await connection.channel(publisher_confirms=True)
        await channel.default_exchange.publish(aio_pika.Message(body, headers=headers), routing_key=queue)


async def _depth(url: str, queue: str) -> int:
    connection = await aio_pika.connect_robust(url)
    async with connection:
        q = await (await connection.channel()).declare_queue(queue, passive=True)
        return q.declaration_result.message_count or 0


async def _take(url: str, queue: str) -> aio_pika.abc.AbstractIncomingMessage | None:
    connection = await aio_pika.connect_robust(url)
    async with connection:
        q = await (await connection.channel()).declare_queue(queue, passive=True)
        message = await q.get(no_ack=True, fail=False)
        return message


@pytest.mark.integration
@pytest.mark.security
def test_dead_letters_can_be_viewed_replayed_and_discarded_with_audit(infra, sdb, tokens):
    client, url = infra
    queue = "imports.process"
    declare_dead_letter_queues(url, (queue,))

    async def setup() -> None:  # the live task queue the replay returns to
        connection = await aio_pika.connect_robust(url)
        async with connection:
            channel = await connection.channel()
            exchange = await channel.declare_exchange("nexus.tasks", aio_pika.ExchangeType.TOPIC, durable=True)
            live = await channel.declare_queue(queue, durable=True, arguments={"x-queue-type": "quorum"})
            await live.bind(exchange, routing_key=queue)

    asyncio.run(setup())
    body = json.dumps(
        [[], {"ctx": {"tenant_id": TENANT, "correlation_id": "corr-1"}, "import_id": "imp-1"}, {}]
    ).encode()
    for _ in range(3):
        asyncio.run(_publish(url, f"{queue}.dlq", body, {"task": "imports.process"}))

    owner = _platform_user(sdb, tokens, "platform_owner")
    operator = _platform_user(sdb, tokens, "platform_operator")
    no_mfa_owner = _platform_user(sdb, tokens, "platform_owner", mfa=False)

    # View: metadata only, messages stay in the dead-letter queue.
    seen = client.get("/api/v1/platform/infra/dead-letters", params={"queue": f"{queue}.dlq"}, headers=auth(operator))
    assert seen.status_code == 200, seen.text
    items = seen.json()["items"]
    assert (
        len(items) == 3 and items[0]["name"] == "imports.process" and items[0]["identifiers"] == {"import_id": "imp-1"}
    )
    assert asyncio.run(_depth(url, f"{queue}.dlq")) == 3

    queues = client.get("/api/v1/platform/infra/queues", headers=auth(operator))
    assert queues.status_code == 200 and any(q["name"] == f"{queue}.dlq" for q in queues.json()["items"])

    action = {"queue": f"{queue}.dlq", "count": 2, "reason": "provider outage resolved"}
    # Operators can look but not replay; replay needs the permission AND step-up MFA.
    assert (
        client.post("/api/v1/platform/infra/dead-letters/replay", json=action, headers=auth(operator)).status_code
        == 403
    )
    denied = client.post("/api/v1/platform/infra/dead-letters/replay", json=action, headers=auth(no_mfa_owner))
    assert denied.status_code == 401 and denied.json()["code"] == "step_up_required"
    assert asyncio.run(_depth(url, f"{queue}.dlq")) == 3

    replayed = client.post("/api/v1/platform/infra/dead-letters/replay", json=action, headers=auth(owner))
    assert replayed.status_code == 200, replayed.text
    assert replayed.json() == {
        "queue": f"{queue}.dlq",
        "action": "replayed",
        "processed": 2,
        "target": "nexus.tasks:imports.process",
    }
    assert asyncio.run(_depth(url, f"{queue}.dlq")) == 1 and asyncio.run(_depth(url, queue)) == 2
    back = asyncio.run(_take(url, queue))
    assert back is not None and back.body == body and "x-replay-id" in back.headers

    discarded = client.post(
        "/api/v1/platform/infra/dead-letters/discard",
        json={**action, "count": 5, "reason": "obsolete import"},
        headers=auth(owner),
    )
    assert discarded.status_code == 200 and discarded.json()["processed"] == 1
    assert asyncio.run(_depth(url, f"{queue}.dlq")) == 0

    # Arbitrary queues cannot be drained through this endpoint.
    bad = client.post(
        "/api/v1/platform/infra/dead-letters/replay",
        json={"queue": "imports.process", "count": 1, "reason": "not a dlq"},
        headers=auth(owner),
    )
    assert bad.status_code == 422

    async def audit_actions() -> list[str]:
        database = Database(sdb.app_url, pool_size=1)
        async with database.global_session() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            "SELECT envelope->'payload'->>'action' FROM outbox WHERE event_type = 'audit.event' "
                            "AND envelope->'payload'->>'action' LIKE 'platform.dlq.%' ORDER BY id"
                        )
                    )
                )
                .scalars()
                .all()
            )
        await database.dispose()
        return list(rows)

    assert asyncio.run(audit_actions())[-2:] == ["platform.dlq.replayed", "platform.dlq.discarded"]


@pytest.mark.integration
def test_tenant_users_cannot_reach_infrastructure_endpoints(infra, tokens):
    client, _ = infra
    user = tokens.user(uuid.uuid4(), "someone@example.test", mfa=True)
    for path in ("/api/v1/platform/infra/services", "/api/v1/platform/infra/queues"):
        assert client.get(path, headers=auth(user)).status_code == 403
    assert client.get("/api/v1/platform/infra/queues").status_code == 401

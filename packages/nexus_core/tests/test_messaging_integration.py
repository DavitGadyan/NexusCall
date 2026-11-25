"""Outbox relay, idempotent event consumption and Celery task delivery against real RabbitMQ + PostgreSQL.

Covers build.md §12.1: duplicate delivery, retry, poison/malformed messages and dead-letter behaviour.
Requires Docker.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid

import aio_pika
import pytest
import pytest_asyncio
from sqlalchemy import Column, Integer, MetaData, String, Table, select, text
from sqlalchemy.dialects.postgresql import UUID

from nexus_core.celery_app import (
    JobContext,
    PermanentFailure,
    TransientFailure,
    create_celery,
    declare_dead_letter_queues,
)
from nexus_core.db import Database
from nexus_core.messaging import DLX_EXCHANGE, EVENTS_EXCHANGE, EventConsumer, OutboxRelay, PermanentEventError, enqueue
from nexus_core.tables import inbox_table, outbox_table
from nexus_events import ActorRef, make_event

pytestmark = [pytest.mark.integration]
ASYNC = pytest.mark.asyncio(loop_scope="module")

metadata = MetaData()
outbox = outbox_table(metadata)
inbox = inbox_table(metadata)
effects = Table(
    "effects",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tenant_id", UUID(as_uuid=True)),
    Column("campaign_id", String(64)),
)
TENANT = uuid.uuid4()


@pytest.fixture(scope="module")
def rabbit():
    tc = pytest.importorskip("testcontainers.rabbitmq")
    try:
        container = tc.RabbitMqContainer("rabbitmq:4.0.5-management-alpine")
        container.start()
    except Exception as exc:  # noqa: BLE001 - skip when Docker is unavailable
        pytest.skip(f"Docker unavailable: {exc}")
    host, port = container.get_container_host_ip(), container.get_exposed_port(5672)
    yield f"amqp://guest:guest@{host}:{port}//"
    container.stop()


@pytest.fixture(scope="module")
def pg_url():
    from nexus_core.testing import start_postgres

    try:
        container = start_postgres("postgres:16-alpine")
    except Exception as exc:  # noqa: BLE001 - skip when Docker is unavailable
        pytest.skip(f"Docker unavailable: {exc}")
    host, port = container.get_container_host_ip(), container.get_exposed_port(5432)
    yield f"postgresql+asyncpg://{container.username}:{container.password}@{host}:{port}/{container.dbname}"
    container.stop()


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def db(pg_url):
    database = Database(pg_url, pool_size=3)
    async with database.engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield database
    await database.dispose()


def _event(event_type: str = "campaign.launched", producer: str = "campaign-service", campaign_id: str | None = None):
    return make_event(
        event_type,
        producer=producer,
        actor=ActorRef(type="system", id="test"),
        correlation_id=uuid.uuid4(),
        tenant_id=TENANT,
        payload={"campaign_id": campaign_id or str(uuid.uuid4())},
    )


async def _effect_count(db: Database, campaign_id: str) -> int:
    async with db.global_session() as s:
        return len((await s.execute(select(effects).where(effects.c.campaign_id == campaign_id))).all())


async def _wait_for(predicate, wait_seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.2)
    return False


async def _queue_depth(url: str, queue: str) -> int:
    connection = await aio_pika.connect_robust(url)
    async with connection:
        channel = await connection.channel()
        q = await channel.declare_queue(queue, passive=True)
        return q.declaration_result.message_count or 0


@ASYNC
async def test_outbox_relay_publishes_and_consumer_dedupes_duplicates(db, rabbit):
    name = f"consumer_{uuid.uuid4().hex[:8]}"
    seen_tenants: list[str] = []

    async def handler(event, session):
        seen_tenants.append((await session.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar_one())
        await session.execute(
            effects.insert().values(tenant_id=event.tenant_id, campaign_id=event.payload["campaign_id"])
        )

    consumer = EventConsumer(
        consumer_name=name,
        db=db,
        inbox=inbox,
        amqp_url=rabbit,
        handlers={"campaign.launched": handler},
        allowed_producers={"campaign.launched": {"campaign-service"}},
    )
    task = asyncio.create_task(consumer.run())
    try:
        await asyncio.sleep(1.0)  # queue declared and bound
        event = _event()
        campaign_id = event.payload["campaign_id"]
        async with db.global_session() as s:  # business change + outbox row in one transaction
            await enqueue(s, outbox, event)
        connection = await aio_pika.connect_robust(rabbit)
        async with connection:
            channel = await connection.channel(publisher_confirms=True)
            exchange = await channel.declare_exchange(EVENTS_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
            relay = OutboxRelay(db, outbox, rabbit)
            assert await relay.drain_once(exchange) == 1
            assert await relay.drain_once(exchange) == 0  # already marked published
            # At-least-once delivery: the broker may deliver the same event again.
            body = event.model_dump_json().encode()
            for _ in range(3):
                await exchange.publish(
                    aio_pika.Message(body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                    routing_key="campaign.launched",
                )
            # A forged event from a producer that may not emit this type is ignored.
            forged = _event(producer="crm-service")
            await exchange.publish(aio_pika.Message(forged.model_dump_json().encode()), routing_key="campaign.launched")
        assert await _wait_for(lambda: _effect_count_ge(db, campaign_id, 1))
        await asyncio.sleep(1.5)  # let the duplicates and the forged event be processed
        assert await _effect_count(db, campaign_id) == 1, "duplicate deliveries must not repeat the side effect"
        assert await _effect_count(db, forged.payload["campaign_id"]) == 0
        assert seen_tenants == [str(TENANT)], "handler must run inside the event's tenant context"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _effect_count_ge(db: Database, campaign_id: str, n: int) -> bool:
    return await _effect_count(db, campaign_id) >= n


@ASYNC
async def test_poison_and_malformed_events_are_dead_lettered(db, rabbit):
    name = f"consumer_{uuid.uuid4().hex[:8]}"
    attempts = {"transient": 0}

    async def always_fails(event, session):
        attempts["transient"] += 1
        raise RuntimeError("boom")

    async def permanent(event, session):
        raise PermanentEventError

    consumer = EventConsumer(
        consumer_name=name,
        db=db,
        inbox=inbox,
        amqp_url=rabbit,
        prefetch=1,
        handlers={"campaign.paused": always_fails, "campaign.canceled": permanent},
    )
    task = asyncio.create_task(consumer.run())
    try:
        await asyncio.sleep(1.0)
        connection = await aio_pika.connect_robust(rabbit)
        async with connection:
            channel = await connection.channel(publisher_confirms=True)
            exchange = await channel.declare_exchange(EVENTS_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
            await exchange.publish(
                aio_pika.Message(_event("campaign.paused").model_dump_json().encode()), routing_key="campaign.paused"
            )
            await exchange.publish(
                aio_pika.Message(_event("campaign.canceled").model_dump_json().encode()),
                routing_key="campaign.canceled",
            )
            await exchange.publish(aio_pika.Message(b"{not json"), routing_key="campaign.paused")
        dlq = f"events.{name}.dlq"

        async def three_dead() -> bool:
            return await _queue_depth(rabbit, dlq) >= 3

        assert await _wait_for(three_dead, wait_seconds=30), "failed, permanent and malformed events must reach the DLQ"
        assert 2 <= attempts["transient"] <= 6, "transient failures are retried a bounded number of times"
        assert await _queue_depth(rabbit, f"events.{name}") == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ------------------------------------------------------------------------------------------- Celery
def test_celery_tasks_on_quorum_queues_retry_and_dead_letter(rabbit):
    queue = "imports.process"
    app = create_celery("test-worker", rabbit, queues=(queue,))
    declare_dead_letter_queues(rabbit, (queue,))
    calls: dict[str, int] = {"ok": 0, "flaky": 0, "bug": 0, "permanent": 0}
    done = threading.Event()

    @app.task(name="t.ok", queue=queue)
    def ok(ctx: dict, thing_id: str) -> None:
        JobContext(**ctx)
        calls["ok"] += 1

    @app.task(
        name="t.flaky", queue=queue, max_retries=3, retry_backoff=False, default_retry_delay=1, retry_jitter=False
    )
    def flaky(ctx: dict, thing_id: str) -> None:
        calls["flaky"] += 1
        if calls["flaky"] < 2:
            raise TransientFailure("provider timeout")
        done.set()

    @app.task(name="t.bug", queue=queue, max_retries=0)
    def bug(ctx: dict, thing_id: str) -> None:
        calls["bug"] += 1
        raise RuntimeError("unexpected")

    @app.task(name="t.permanent", queue=queue)
    def permanent(ctx: dict, thing_id: str) -> None:
        calls["permanent"] += 1
        try:
            raise PermanentFailure("invalid recipient")
        except PermanentFailure:
            return  # convention: record the failure in the owning DB and finish (no retry, no DLQ)

    from celery.contrib.testing.worker import start_worker

    ctx = JobContext(tenant_id=TENANT, correlation_id=uuid.uuid4()).model_dump(mode="json")
    with start_worker(app, pool="solo", perform_ping_check=False, loglevel="WARNING", shutdown_timeout=30):
        ok.apply_async(kwargs={"ctx": ctx, "thing_id": "1"})
        flaky.apply_async(kwargs={"ctx": ctx, "thing_id": "2"})
        permanent.apply_async(kwargs={"ctx": ctx, "thing_id": "3"})
        bug.apply_async(kwargs={"ctx": ctx, "thing_id": "4"})
        assert done.wait(40), "a TransientFailure must be retried (delayed delivery on quorum queues) until success"
        deadline = time.monotonic() + 30
        dead = 0
        while time.monotonic() < deadline and dead < 1:
            dead = asyncio.run(_queue_depth(rabbit, f"{queue}.dlq"))
            time.sleep(0.5)
    assert calls["ok"] == 1 and calls["flaky"] == 2 and calls["permanent"] == 1
    assert calls["bug"] >= 1
    assert dead == 1, "an unexpected failure with no retries left must be dead-lettered, not silently acked"
    body = asyncio.run(_peek(rabbit, f"{queue}.dlq"))
    assert "password" not in body and json.loads(body)[1]["thing_id"] == "4", "task payloads carry ids only"


async def _peek(url: str, queue: str) -> str:
    connection = await aio_pika.connect_robust(url)
    async with connection:
        channel = await connection.channel()
        q = await channel.declare_queue(queue, passive=True)
        message = await q.get(timeout=5)
        assert message is not None
        await message.nack(requeue=True)
        return message.body.decode()


def test_dlx_constant_matches_between_events_and_tasks():
    from nexus_core import celery_app

    assert celery_app.DLX_EXCHANGE == DLX_EXCHANGE

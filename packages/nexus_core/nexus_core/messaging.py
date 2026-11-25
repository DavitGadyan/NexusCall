"""Outbox enqueue/relay and idempotent event consumption over RabbitMQ (ADR-0008).

* `enqueue` writes the event into the service's outbox in the caller's transaction.
* `OutboxRelay` drains unpublished rows (FOR UPDATE SKIP LOCKED) and publishes
  with publisher confirms; only then marks them published.
* `EventConsumer` binds a durable quorum queue, establishes tenant RLS context,
  dedupes with the inbox table in the same transaction as the side effects, and
  acks manually. Poison messages are dead-lettered after bounded retries.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import aio_pika
from aio_pika.abc import AbstractIncomingMessage, AbstractRobustConnection
from sqlalchemy import Table, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_core.context import Actor, correlation_id_var, tenant_id_var
from nexus_core.db import Database
from nexus_events import EventEnvelope

log = logging.getLogger(__name__)

EVENTS_EXCHANGE = "nexus.events"
DLX_EXCHANGE = "nexus.dlx"
MAX_DELIVERIES = 5


async def enqueue(session: AsyncSession, outbox: Table, event: EventEnvelope) -> None:
    await session.execute(
        insert(outbox).values(
            event_id=event.event_id,
            tenant_id=event.tenant_id,
            event_type=event.event_type,
            event_version=event.event_version,
            envelope=json.loads(event.model_dump_json()),
        )
    )


class OutboxRelay:
    def __init__(self, db: Database, outbox: Table, amqp_url: str, *, batch: int = 100, idle_sleep: float = 0.5):
        self.db, self.t, self.url, self.batch, self.idle = db, outbox, amqp_url, batch, idle_sleep
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def drain_once(self, exchange: aio_pika.abc.AbstractExchange) -> int:
        async with self.db.global_session() as s:
            rows = (
                await s.execute(
                    select(self.t.c.id, self.t.c.event_type, self.t.c.envelope)
                    .where(self.t.c.published_at.is_(None))
                    .order_by(self.t.c.id)
                    .limit(self.batch)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for row in rows:
                body = json.dumps(row.envelope).encode()
                env = row.envelope
                msg = aio_pika.Message(
                    body,
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    message_id=str(env["event_id"]),
                    type=row.event_type,
                    headers={"correlation_id": env.get("correlation_id"), "tenant_id": env.get("tenant_id")},
                )
                try:
                    await exchange.publish(msg, routing_key=row.event_type)  # confirm awaited
                except Exception as exc:  # noqa: BLE001 - recorded; message retried or dead-lettered
                    await s.execute(
                        update(self.t)
                        .where(self.t.c.id == row.id)
                        .values(attempts=self.t.c.attempts + 1, last_error=type(exc).__name__)
                    )
                    log.warning("outbox_publish_failed", extra={"outbox_id": row.id})
                    break
                await s.execute(update(self.t).where(self.t.c.id == row.id).values(published_at=_now_sql()))
            return len(rows)

    async def run(self) -> None:
        connection = await aio_pika.connect_robust(self.url)
        async with connection:
            channel = await connection.channel(publisher_confirms=True)
            exchange = await channel.declare_exchange(EVENTS_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
            while not self._stop.is_set():
                n = await self.drain_once(exchange)
                if n == 0:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=self.idle)


def _now_sql() -> Any:
    from sqlalchemy import func

    return func.now()


Handler = Callable[[EventEnvelope, AsyncSession], Awaitable[None]]


class PermanentEventError(Exception):
    """Raised by handlers for events that must not be retried (dead-lettered immediately)."""


class EventConsumer:
    def __init__(
        self,
        *,
        consumer_name: str,
        db: Database,
        inbox: Table,
        amqp_url: str,
        handlers: dict[str, Handler],
        allowed_producers: dict[str, set[str]] | None = None,
        prefetch: int = 10,
    ) -> None:
        self.name, self.db, self.inbox, self.url = consumer_name, db, inbox, amqp_url
        self.handlers = handlers
        # event_type -> producers allowed to emit it (defense against forged events; build.md §6.3).
        self.allowed_producers = allowed_producers or {}
        self.prefetch = prefetch
        self._connection: AbstractRobustConnection | None = None

    async def handle_message(self, message: AbstractIncomingMessage) -> None:
        try:
            event = EventEnvelope.model_validate_json(message.body)
        except Exception:  # noqa: BLE001 - recorded; message retried or dead-lettered
            log.error("event_malformed", extra={"consumer": self.name})
            await message.reject(requeue=False)
            return
        handler = self.handlers.get(event.event_type)
        producers = self.allowed_producers.get(event.event_type)
        if handler is None or (producers is not None and event.producer not in producers):
            log.warning("event_ignored", extra={"event_type": event.event_type, "producer": event.producer})
            await message.ack()
            return
        correlation_id_var.set(str(event.correlation_id))
        tenant_id_var.set(str(event.tenant_id) if event.tenant_id else None)
        # The consumer acts under its own service identity; the original actor stays in the envelope.
        actor = Actor(type="service", id=self.name)
        try:
            ctx = self.db.tenant_session(event.tenant_id, actor) if event.tenant_id else self.db.global_session(actor)
            async with ctx as session:
                claimed = (
                    await session.execute(
                        insert(self.inbox)
                        .values(
                            consumer=self.name,
                            event_id=event.event_id,
                            tenant_id=event.tenant_id,
                            event_type=event.event_type,
                        )
                        .on_conflict_do_nothing()
                        .returning(self.inbox.c.event_id)
                    )
                ).scalar_one_or_none()
                if claimed is None:
                    log.info("event_duplicate_skipped", extra={"event_id": str(event.event_id)})
                else:
                    await handler(event, session)
            await message.ack()
        except PermanentEventError:
            log.error("event_permanent_failure", extra={"event_id": str(event.event_id)})
            await message.reject(requeue=False)
        except Exception:
            raw_count = (message.headers or {}).get("x-delivery-count", 0)
            deliveries = int(raw_count) if isinstance(raw_count, int | str) else 0
            log.exception("event_handler_failed", extra={"event_id": str(event.event_id), "deliveries": deliveries})
            await message.reject(requeue=deliveries + 1 < MAX_DELIVERIES)

    async def run(self) -> None:
        self._connection = await aio_pika.connect_robust(self.url)
        channel = await self._connection.channel()
        await channel.set_qos(prefetch_count=self.prefetch)
        exchange = await channel.declare_exchange(EVENTS_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
        await channel.declare_exchange(DLX_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
        queue_name = f"events.{self.name}"
        dlq = await channel.declare_queue(f"{queue_name}.dlq", durable=True, arguments={"x-queue-type": "quorum"})
        await dlq.bind(DLX_EXCHANGE, routing_key=queue_name)
        queue = await channel.declare_queue(
            queue_name,
            durable=True,
            arguments={
                "x-queue-type": "quorum",
                "x-dead-letter-exchange": DLX_EXCHANGE,
                "x-dead-letter-routing-key": queue_name,
                "x-delivery-limit": MAX_DELIVERIES,
            },
        )
        for event_type in self.handlers:
            await queue.bind(exchange, routing_key=event_type)
        await queue.consume(self.handle_message)
        await asyncio.Future()


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()

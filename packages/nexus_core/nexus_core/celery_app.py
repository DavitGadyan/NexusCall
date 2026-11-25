"""Celery factory and task conventions (ADR-0011).

Every task receives a serialized `JobContext` (tenant, correlation, actor, job id)
and only identifiers. Tasks raise `PermanentFailure` for errors that must not be
retried (invalid recipient, permission denied, opt-out) and `TransientFailure`
for retryable ones; retries use exponential backoff with jitter.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, TypeVar

from celery import Celery, Task
from kombu import Exchange, Queue  # type: ignore[attr-defined]
from pydantic import BaseModel, ConfigDict, Field

from nexus_core.context import Actor, correlation_id_var, tenant_id_var

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from nexus_core.clients import ServiceClient
    from nexus_core.config import ServiceSettings
    from nexus_core.db import Database
    from nexus_core.storage import ObjectStore
    from nexus_core.vault import VaultClient

T = TypeVar("T")

QUEUES = (
    "calls.schedule",
    "calls.dispatch",
    "calls.provider_callbacks",
    "voice.postprocess",
    "emails.campaign_send",
    "emails.inbound_process",
    "emails.ai_draft",
    "emails.send_approved",
    "integrations.webhooks",
    "imports.process",
    "analytics.aggregate",
    "notifications.send",
)
LATENCY_SENSITIVE = frozenset({"calls.dispatch", "calls.provider_callbacks", "emails.send_approved"})


class PermanentFailure(Exception):
    """Do not retry. The task records the failure in the owning DB and exits."""


class TransientFailure(Exception):
    """Retry with backoff."""


class JobContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    job_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    correlation_id: uuid.UUID
    causation_id: uuid.UUID | None = None
    actor_type: str = "system"
    actor_id: str = "system"

    @property
    def actor(self) -> Actor:
        return Actor(type=self.actor_type, id=self.actor_id)

    def child(self) -> JobContext:
        """Context for a follow-up job caused by this one (new job id, same correlation)."""
        return self.model_copy(update={"job_id": uuid.uuid4(), "causation_id": self.job_id})


class NexusTask(Task):  # type: ignore[misc]
    autoretry_for = (TransientFailure, ConnectionError, TimeoutError)
    dont_autoretry_for = (PermanentFailure,)
    retry_backoff = 2
    retry_backoff_max = 300
    retry_jitter = True
    max_retries = 5
    acks_late = True
    reject_on_worker_lost = True
    ignore_result = True

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        ctx = kwargs.get("ctx")
        if isinstance(ctx, dict):
            correlation_id_var.set(str(ctx.get("correlation_id")))
            tenant_id_var.set(str(ctx.get("tenant_id")))
        return super().__call__(*args, **kwargs)


DLX_EXCHANGE = "nexus.dlx"


def declare_dead_letter_queues(broker_url: str, queues: tuple[str, ...] = QUEUES) -> None:
    """Declare `<queue>.dlq` (quorum) bound to the dead-letter exchange. Idempotent; run at bootstrap."""
    from kombu import Connection

    dlx = Exchange(DLX_EXCHANGE, type="topic", durable=True)  # type: ignore[no-untyped-call]
    with Connection(broker_url) as conn:  # type: ignore[no-untyped-call]
        channel = conn.channel()
        dlx(channel).declare()
        for q in queues:
            Queue(  # type: ignore[no-untyped-call]
                f"{q}.dlq", dlx, routing_key=f"{q}.dlq", durable=True, queue_arguments={"x-queue-type": "quorum"}
            )(channel).declare()


def create_celery(name: str, broker_url: str, *, queues: tuple[str, ...] = QUEUES) -> Celery:
    app = Celery(name, broker=broker_url, task_cls=NexusTask)
    # Topic exchange with exact routing keys: Celery's native delayed delivery (used for countdown/retry
    # on quorum queues) does not support direct exchanges.
    tasks_exchange = Exchange("nexus.tasks", type="topic", durable=True)  # type: ignore[no-untyped-call]
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_backend=None,
        task_ignore_result=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        # Exhausted/unexpected failures are rejected (not acked) so RabbitMQ dead-letters them to <queue>.dlq.
        task_acks_on_failure_or_timeout=False,
        worker_prefetch_multiplier=1,
        broker_transport_options={"confirm_publish": True},
        broker_connection_retry_on_startup=True,
        task_default_exchange="nexus.tasks",
        task_default_exchange_type="topic",
        task_default_queue_type="quorum",
        broker_native_delayed_delivery_queue_type="quorum",
        task_default_delivery_mode="persistent",
        task_time_limit=300,
        task_soft_time_limit=240,
        worker_hijack_root_logger=False,
        worker_enable_remote_control=False,
        worker_send_task_events=False,
        task_send_sent_event=False,
        task_queues=[
            Queue(  # type: ignore[no-untyped-call]
                q,
                tasks_exchange,
                routing_key=q,
                durable=True,
                queue_arguments={
                    "x-queue-type": "quorum",
                    "x-dead-letter-exchange": DLX_EXCHANGE,
                    "x-dead-letter-routing-key": f"{q}.dlq",
                    "x-delivery-limit": 6,
                },
            )
            for q in queues
        ],
    )
    return app


async def enqueue_task(
    app: Celery, name: str, queue: str, ctx: JobContext, *, countdown: float | None = None, **ids: str
) -> None:
    """Publish a task with IDs only (never payload bodies or secrets). Call AFTER the DB commit.

    The business row must already record the job as `queued`; a periodic sweeper re-enqueues rows that
    stay queued too long, so a crash between commit and publish cannot lose work (tasks are idempotent)."""
    for key, value in ids.items():
        if not isinstance(value, str) or len(value) > 200:
            raise ValueError(f"Task argument {key!r} must be a short string identifier")
    kwargs = {"ctx": ctx.model_dump(mode="json"), **ids}
    await asyncio.to_thread(lambda: app.send_task(name, kwargs=kwargs, queue=queue, countdown=countdown))


class TaskRuntime:
    """Per-worker-process resources for Celery tasks: one event loop, DB pool, Redis, Vault, service clients.

    Usage in `<pkg>/worker.py`:

        celery_app = create_celery("call-service", get_settings().amqp_url.get_secret_value())
        runtime = TaskRuntime(get_settings)

        @celery_app.task(name="calls.dispatch", queue="calls.dispatch")
        def dispatch(ctx: dict, call_id: str) -> None:
            runtime.run(dispatch_call(runtime, JobContext(**ctx), uuid.UUID(call_id)))

    Keep the coroutine (`dispatch_call`) free of Celery so it can be unit-tested directly.
    """

    def __init__(self, settings_factory: Callable[[], ServiceSettings]) -> None:
        self._settings_factory = settings_factory
        self._pid: int | None = None
        self._reset()

    def _reset(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._db: Database | None = None
        self._redis: Redis | None = None
        self._vault: VaultClient | None = None
        self._store: ObjectStore | None = None
        self._token: Callable[[], Awaitable[str]] | None = None
        self._clients: dict[str, ServiceClient] = {}
        self._pid = os.getpid()

    def _ensure_process(self) -> None:
        if self._pid != os.getpid():  # forked worker child: never reuse the parent's loop or sockets
            self._reset()

    @property
    def settings(self) -> ServiceSettings:
        return self._settings_factory()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        self._ensure_process()
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        return self.loop.run_until_complete(coro)

    @property
    def db(self) -> Database:
        self._ensure_process()
        if self._db is None:
            from nexus_core.db import Database

            self._db = Database(self.settings.database_url.get_secret_value(), pool_size=4, max_overflow=2)
        return self._db

    @property
    def redis(self) -> Redis:
        self._ensure_process()
        if self._redis is None:
            from redis.asyncio import Redis

            self._redis = Redis.from_url(self.settings.redis_url.get_secret_value())
        return self._redis

    @property
    def vault(self) -> VaultClient:
        self._ensure_process()
        if self._vault is None:
            from nexus_core.vault import VaultClient

            s = self.settings
            self._vault = VaultClient(
                s.vault_addr,
                kv_mount=s.vault_kv_mount,
                role_id_file=s.vault_role_id_file,
                secret_id_file=s.vault_secret_id_file,
                token_file=s.vault_token_file,
            )
        return self._vault

    @property
    def store(self) -> ObjectStore:
        self._ensure_process()
        if self._store is None:
            from nexus_core.storage import ObjectStore

            self._store = ObjectStore.from_settings(self.settings)
        return self._store

    async def service_token(self) -> str:
        self._ensure_process()
        if self._token is None:
            from nexus_core.auth.service_token import ServiceTokenProvider

            s = self.settings
            secret = s.client_secret_value()
            if not (s.service_client_id and secret):
                raise RuntimeError("Service client credentials are not configured")
            self._token = ServiceTokenProvider(s.token_url, s.service_client_id, secret).token
        return await self._token()

    def client(self, service: str, *, timeout: float = 10.0) -> ServiceClient:
        self._ensure_process()
        if service not in self._clients:
            from nexus_core.clients import ServiceClient

            self._clients[service] = ServiceClient(
                service, self.settings.service_url(service), self.service_token, timeout=timeout
            )
        return self._clients[service]

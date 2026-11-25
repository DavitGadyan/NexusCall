"""Runtime dependencies shared by the media socket, request handlers, Celery tasks and periodic jobs."""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_core.audit import emit_audit
from nexus_core.celery_app import JobContext
from nexus_core.clients import ServiceClient, service_client
from nexus_core.config import ProviderMode
from nexus_core.context import Actor, current_correlation_id
from nexus_core.credentials import CredentialResolver
from nexus_core.db import Database
from nexus_core.messaging import enqueue
from nexus_core.storage import ObjectStore
from nexus_events import ActorRef, make_event
from voice_service.settings import Settings
from voice_service.tables import outbox

PRODUCER = "voice-service"
SERVICE_ACTOR = Actor("service", PRODUCER)


class Deps(Protocol):
    settings: Settings
    db: Database
    redis: Redis | None
    store: ObjectStore
    credentials: CredentialResolver

    def client(self, service: str) -> ServiceClient: ...

    async def enqueue(self, name: str, queue: str, ctx: JobContext, **ids: str) -> None: ...


async def celery_enqueue(name: str, queue: str, ctx: JobContext, **ids: str) -> None:
    from nexus_core.celery_app import enqueue_task
    from voice_service.worker import celery_app

    await enqueue_task(celery_app, name, queue, ctx, **ids)


class AppDeps:
    def __init__(self, app: Any) -> None:
        self.app = app

    @property
    def settings(self) -> Settings:
        return self.app.state.settings  # type: ignore[no-any-return]

    @property
    def db(self) -> Database:
        return self.app.state.db  # type: ignore[no-any-return]

    @property
    def redis(self) -> Redis | None:
        return getattr(self.app.state, "redis", None)

    @property
    def store(self) -> ObjectStore:
        return self.app.state.store  # type: ignore[no-any-return]

    @property
    def credentials(self) -> CredentialResolver:
        return self.app.state.credentials  # type: ignore[no-any-return]

    def client(self, service: str) -> ServiceClient:
        return service_client(self.app, service, timeout=10.0)

    async def enqueue(self, name: str, queue: str, ctx: JobContext, **ids: str) -> None:
        await self.app.state.enqueue(name, queue, ctx, **ids)


class WorkerDeps:
    def __init__(self, runtime: Any, settings: Settings) -> None:
        self.runtime, self.settings = runtime, settings
        self._credentials: CredentialResolver | None = None

    @property
    def db(self) -> Database:
        return self.runtime.db  # type: ignore[no-any-return]

    @property
    def redis(self) -> Redis | None:
        return self.runtime.redis  # type: ignore[no-any-return]

    @property
    def store(self) -> ObjectStore:
        return self.runtime.store  # type: ignore[no-any-return]

    @property
    def credentials(self) -> CredentialResolver:
        if self._credentials is None:
            fake = self.settings.provider_mode is ProviderMode.FAKE
            self._credentials = CredentialResolver(
                self.settings,
                None if fake else self.runtime.vault,
                None if fake else self.runtime.client("integration-service"),
            )
        return self._credentials

    def client(self, service: str) -> ServiceClient:
        return self.runtime.client(service, timeout=30.0)  # type: ignore[no-any-return]

    async def enqueue(self, name: str, queue: str, ctx: JobContext, **ids: str) -> None:
        await celery_enqueue(name, queue, ctx, **ids)


async def emit(
    session: AsyncSession, event_type: str, *, tenant_id: uuid.UUID, actor: Actor, payload: dict[str, Any]
) -> None:
    event = make_event(
        event_type,
        producer=PRODUCER,
        actor=ActorRef(type=actor.type, id=actor.id),  # type: ignore[arg-type]
        correlation_id=current_correlation_id(),
        tenant_id=tenant_id,
        payload=payload,
    )
    await enqueue(session, outbox, event)


async def audit(
    session: AsyncSession,
    actor: Actor,
    action: str,
    resource_type: str,
    resource_id: Any,
    *,
    tenant_id: uuid.UUID,
    outcome: str = "success",
    **metadata: Any,
) -> None:
    await emit_audit(
        session,
        outbox,
        producer=PRODUCER,
        actor=actor,
        tenant_id=tenant_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        metadata=metadata,
    )


def job_ctx(tenant_id: uuid.UUID, actor: Actor = SERVICE_ACTOR) -> JobContext:
    try:
        correlation = uuid.UUID(current_correlation_id())
    except ValueError:
        correlation = uuid.uuid4()
    return JobContext(tenant_id=tenant_id, correlation_id=correlation, actor_type=actor.type, actor_id=actor.id)

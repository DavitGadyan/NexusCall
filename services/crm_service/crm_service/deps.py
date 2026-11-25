"""Request-scoped helpers shared by the routers."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from crm_service.tables import outbox
from nexus_core.audit import emit_audit
from nexus_core.celery_app import JobContext
from nexus_core.context import Actor, current_correlation_id
from nexus_core.db import Database
from nexus_core.messaging import enqueue
from nexus_core.storage import ObjectStore
from nexus_events import ActorRef, make_event

PRODUCER = "crm-service"
log = logging.getLogger(__name__)


def db(request: Request) -> Database:
    return request.app.state.db  # type: ignore[no-any-return]


def redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


def store(request: Request) -> ObjectStore:
    value = getattr(request.app.state, "object_store", None)
    if value is None:
        from nexus_core.errors import ServiceUnavailable

        raise ServiceUnavailable("Object storage is not configured")
    return value  # type: ignore[no-any-return]


async def audit(
    session: AsyncSession,
    actor: Actor,
    action: str,
    resource_type: str,
    resource_id: Any = None,
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


async def emit(
    session: AsyncSession, event_type: str, *, tenant_id: uuid.UUID, actor: Actor, payload: dict[str, Any]
) -> None:
    """Domain event through the transactional outbox (ids and enums only)."""
    event = make_event(
        event_type,
        producer=PRODUCER,
        actor=ActorRef(type=actor.type, id=actor.id),  # type: ignore[arg-type]
        correlation_id=current_correlation_id(),
        tenant_id=tenant_id,
        payload=payload,
    )
    await enqueue(session, outbox, event)


def job_context(tenant_id: uuid.UUID, actor: Actor) -> JobContext:
    return JobContext(
        tenant_id=tenant_id,
        correlation_id=uuid.UUID(current_correlation_id()),
        actor_type=actor.type,
        actor_id=actor.id,
    )


async def enqueue_job(request: Request, name: str, ctx: JobContext, **ids: str) -> None:
    """Publish after commit. A failure is logged only: the sweeper re-enqueues rows still `queued`."""
    try:
        await request.app.state.enqueue(name, "imports.process", ctx, **ids)
    except Exception:  # noqa: BLE001 - the sweeper recovers; never fail the committed request
        log.warning("task_enqueue_failed", extra={"task": name})

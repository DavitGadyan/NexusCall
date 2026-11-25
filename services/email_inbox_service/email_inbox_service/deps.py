"""Runtime dependencies shared by the API process and the Celery workers.

Domain coroutines (`inbound.process_inbound`, `drafting.generate_draft`, `sending.send_approved`) take a
`Deps`, never a FastAPI request or a Celery task, so tests call them directly.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import FastAPI, Request
from redis.asyncio import Redis
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from email_inbox_service import SERVICE_NAME
from email_inbox_service.settings import Settings
from email_inbox_service.tables import draft_actions, outbox
from nexus_core.audit import emit_audit
from nexus_core.celery_app import JobContext
from nexus_core.clients import ServiceClient, service_client
from nexus_core.context import Actor, current_correlation_id
from nexus_core.db import Database
from nexus_core.messaging import enqueue as enqueue_event
from nexus_core.realtime import publish_ui_event
from nexus_core.storage import ObjectStore
from nexus_events import ActorRef, make_event

PRODUCER = SERVICE_NAME


class Enqueue(Protocol):
    def __call__(self, name: str, queue: str, ctx: JobContext, **ids: str) -> Awaitable[None]: ...


@dataclass(slots=True)
class Deps:
    settings: Settings
    db: Database
    redis: Redis | None
    store: ObjectStore | None
    client: Callable[[str], ServiceClient]
    enqueue: Enqueue


def app_deps(app: FastAPI) -> Deps:
    return Deps(
        settings=app.state.settings,
        db=app.state.db,
        redis=getattr(app.state, "redis", None),
        store=getattr(app.state, "object_store", None),
        client=lambda name: service_client(app, name, timeout=15.0),
        enqueue=app.state.enqueue,
    )


def request_deps(request: Request) -> Deps:
    return app_deps(request.app)


def job_context(tenant_id: uuid.UUID, actor: Actor, causation_id: uuid.UUID | None = None) -> JobContext:
    return JobContext(
        tenant_id=tenant_id,
        correlation_id=uuid.UUID(current_correlation_id()),
        causation_id=causation_id,
        actor_type=actor.type,
        actor_id=actor.id,
    )


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


async def event(
    session: AsyncSession, event_type: str, *, tenant_id: uuid.UUID, actor: Actor, payload: dict[str, Any]
) -> None:
    await enqueue_event(
        session,
        outbox,
        make_event(
            event_type,
            producer=PRODUCER,
            actor=ActorRef(type=actor.type, id=actor.id),  # type: ignore[arg-type]
            correlation_id=current_correlation_id(),
            tenant_id=tenant_id,
            payload=payload,
        ),
    )


async def record_action(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    draft_id: uuid.UUID,
    action: str,
    actor: Actor,
    version: int | None = None,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Append to the immutable review history (draft_actions is append-only in the database)."""
    await session.execute(
        insert(draft_actions).values(
            tenant_id=tenant_id,
            draft_id=draft_id,
            action=action,
            version=version,
            actor_type=actor.type,
            actor_id=actor.id[:128],
            reason=reason,
            details=details or {},
        )
    )


async def ui_hint(deps: Deps, tenant_id: uuid.UUID, kind: str, permission: str, **ids: str) -> None:
    """Best-effort live refresh for reviewers (ids only)."""
    try:
        await publish_ui_event(deps.redis, tenant_id, kind, permission=permission, data=ids)
    except Exception:  # noqa: BLE001 - UI hints never affect business state
        return

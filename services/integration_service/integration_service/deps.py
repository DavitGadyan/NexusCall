"""Helpers shared by routers, jobs and the worker: audit, events, tenant index."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from integration_service.settings import Settings
from integration_service.tables import integration_tenants, outbox
from nexus_core.audit import emit_audit
from nexus_core.context import Actor, current_correlation_id
from nexus_core.db import Database
from nexus_core.messaging import enqueue
from nexus_events import ActorRef, make_event

PRODUCER = "integration-service"


def app_of(obj: FastAPI | Request) -> FastAPI:
    return obj.app if isinstance(obj, Request) else obj


def db(obj: FastAPI | Request) -> Database:
    return app_of(obj).state.db  # type: ignore[no-any-return]


def settings_of(obj: FastAPI | Request) -> Settings:
    return app_of(obj).state.settings  # type: ignore[no-any-return]


async def audit(
    session: AsyncSession,
    actor: Actor,
    action: str,
    resource_type: str,
    resource_id: Any,
    *,
    tenant_id: uuid.UUID | None,
    outcome: str = "success",
    **metadata: Any,
) -> None:
    """Audit metadata holds ids and enums only; callers never pass credential material."""
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


def actor_ref(actor: Actor) -> ActorRef:
    kind = actor.type if actor.type in ("user", "service", "system") else "system"
    return ActorRef(type=kind, id=actor.id[:128])  # type: ignore[arg-type]


async def emit_event(
    session: AsyncSession, event_type: str, *, tenant_id: uuid.UUID | None, actor: Actor, payload: dict[str, Any]
) -> None:
    await enqueue(
        session,
        outbox,
        make_event(
            event_type,
            producer=PRODUCER,
            actor=actor_ref(actor),
            correlation_id=current_correlation_id(),
            tenant_id=tenant_id,
            payload=payload,
        ),
    )


async def connection_changed(
    session: AsyncSession, *, tenant_id: uuid.UUID | None, actor: Actor, connection_id: uuid.UUID, provider: str,
    status: str,
) -> None:  # fmt: skip
    await emit_event(
        session,
        "integration.connection.changed",
        tenant_id=tenant_id,
        actor=actor,
        payload={"connection_id": str(connection_id), "provider": provider, "status": status},
    )


async def register_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    await session.execute(insert(integration_tenants).values(tenant_id=tenant_id).on_conflict_do_nothing())

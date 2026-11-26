"""Per-app dependencies and small helpers shared by routers and periodic jobs."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession

from booking_service.settings import Settings
from booking_service.tables import outbox
from nexus_core.audit import emit_audit
from nexus_core.context import Actor, current_correlation_id
from nexus_core.db import Database
from nexus_core.messaging import enqueue
from nexus_core.realtime import publish_ui_event
from nexus_events import ActorRef, make_event

PRODUCER = "booking-service"


def db(app_or_request: FastAPI | Request) -> Database:
    app = app_or_request.app if isinstance(app_or_request, Request) else app_or_request
    return app.state.db  # type: ignore[no-any-return]


def settings_of(app: FastAPI) -> Settings:
    return app.state.settings  # type: ignore[no-any-return]


async def audit(
    session: AsyncSession,
    actor: Actor,
    action: str,
    resource_id: Any,
    *,
    tenant_id: uuid.UUID,
    resource_type: str = "booking",
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


def _actor_ref(actor: Actor) -> ActorRef:
    kind = actor.type if actor.type in ("user", "service", "system") else "system"
    return ActorRef(type=kind, id=actor.id[:128])  # type: ignore[arg-type]


async def emit_event(
    session: AsyncSession, event_type: str, *, tenant_id: uuid.UUID, actor: Actor, payload: dict[str, Any]
) -> None:
    await enqueue(
        session,
        outbox,
        make_event(
            event_type,
            producer=PRODUCER,
            actor=_actor_ref(actor),
            correlation_id=current_correlation_id(),
            tenant_id=tenant_id,
            payload=payload,
        ),
    )


async def booking_event(
    session: AsyncSession,
    event_type: str,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    booking_id: uuid.UUID,
    contact_id: uuid.UUID,
    status: str,
) -> None:
    await emit_event(
        session,
        event_type,
        tenant_id=tenant_id,
        actor=actor,
        payload={"booking_id": str(booking_id), "contact_id": str(contact_id), "status": status},
    )


async def ui_refresh(app: FastAPI, tenant_id: uuid.UUID, booking_id: uuid.UUID) -> None:
    try:
        await publish_ui_event(
            getattr(app.state, "redis", None),
            tenant_id,
            "booking.updated",
            permission="bookings:read",
            data={"booking_id": str(booking_id)},
        )
    except Exception:  # noqa: BLE001 - best-effort UI hint only
        return

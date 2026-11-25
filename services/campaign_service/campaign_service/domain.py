"""Campaign domain operations (always inside a caller-provided tenant session; RLS applies)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, Protocol

from redis.asyncio import Redis
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from campaign_service.state_machine import EVENTS, LIVE, InvalidTransition, next_state
from campaign_service.tables import (
    call_campaigns,
    campaign_contacts,
    campaign_events,
    dispatch_registry,
    kill_switches,
    outbox,
)
from nexus_core.audit import emit_audit
from nexus_core.clients import ServiceClient
from nexus_core.context import Actor, current_correlation_id
from nexus_core.db import Database
from nexus_core.errors import Conflict, NotFound
from nexus_core.messaging import enqueue
from nexus_events import ActorRef, make_event

PRODUCER = "campaign-service"
OPEN_CONTACT_STATES = ("pending", "in_flight", "retry_wait")


class Deps(Protocol):
    """What background code needs: TaskRuntime satisfies it, and so does `AppDeps` for the API process."""

    @property
    def db(self) -> Database: ...

    @property
    def redis(self) -> Redis | None: ...

    def client(self, service: str) -> ServiceClient: ...


class AppDeps:
    def __init__(self, app: Any) -> None:
        from nexus_core.clients import service_client

        self.db: Database = app.state.db
        self.redis: Redis | None = getattr(app.state, "redis", None)
        self._client: Callable[[str], ServiceClient] = lambda name: service_client(app, name)

    def client(self, service: str) -> ServiceClient:
        return self._client(service)


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
    resource_id: Any,
    *,
    tenant_id: uuid.UUID,
    outcome: str = "success",
    resource_type: str = "call_campaign",
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


async def record(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    campaign_id: uuid.UUID,
    kind: str,
    actor: Actor,
    from_state: str | None = None,
    to_state: str | None = None,
    contact_id: uuid.UUID | None = None,
    call_id: uuid.UUID | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    await session.execute(
        insert(campaign_events).values(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            kind=kind,
            from_state=from_state,
            to_state=to_state,
            contact_id=contact_id,
            call_id=call_id,
            actor=str(actor),
            data=data or {},
        )
    )


async def load_campaign(session: AsyncSession, campaign_id: uuid.UUID, *, lock: bool = False) -> dict[str, Any]:
    stmt = select(call_campaigns).where(call_campaigns.c.id == campaign_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).mappings().first()
    if row is None:
        raise NotFound("Campaign not found")
    return dict(row)


async def sync_registry(session: AsyncSession, campaign: dict[str, Any]) -> None:
    """Keep the ids-only dispatch registry in step with the campaign state (same transaction)."""
    if campaign["state"] in LIVE:
        await session.execute(
            insert(dispatch_registry)
            .values(
                campaign_id=campaign["id"],
                tenant_id=campaign["tenant_id"],
                state=campaign["state"],
                snapshot_status=campaign["snapshot_status"],
            )
            .on_conflict_do_update(
                index_elements=["campaign_id"],
                set_={
                    "state": campaign["state"],
                    "snapshot_status": campaign["snapshot_status"],
                    "updated_at": func.now(),
                },
            )
        )
    else:
        await session.execute(delete(dispatch_registry).where(dispatch_registry.c.campaign_id == campaign["id"]))


async def transition(
    session: AsyncSession,
    campaign: dict[str, Any],
    action: str,
    *,
    actor: Actor,
    expected_version: int | None = None,
    values: dict[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Apply a lifecycle action with optimistic locking. Emits the matching domain event, if any."""
    try:
        target = next_state(campaign["state"], action)
    except InvalidTransition as exc:
        raise Conflict(str(exc), extra={"state": campaign["state"]}) from exc
    version = campaign["version"] if expected_version is None else expected_version
    row = (
        (
            await session.execute(
                update(call_campaigns)
                .where(
                    call_campaigns.c.id == campaign["id"],
                    call_campaigns.c.state == campaign["state"],
                    call_campaigns.c.version == version,
                )
                .values(state=target, version=call_campaigns.c.version + 1, updated_at=func.now(), **(values or {}))
                .returning(call_campaigns)
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise Conflict("Campaign was changed by someone else; reload and retry")
    updated = dict(row)
    tenant_id = updated["tenant_id"]
    await record(
        session,
        tenant_id=tenant_id,
        campaign_id=updated["id"],
        kind=f"state.{action}",
        actor=actor,
        from_state=campaign["state"],
        to_state=target,
        data={"reason": reason} if reason else None,
    )
    await sync_registry(session, updated)
    if action in EVENTS:
        await emit(
            session,
            EVENTS[action],
            tenant_id=tenant_id,
            actor=actor,
            payload={"campaign_id": str(updated["id"]), "channel": "call"},
        )
    return updated


async def kill_switch_engaged(session: AsyncSession) -> bool:
    engaged = (
        await session.execute(
            select(kill_switches.c.engaged).where(kill_switches.c.scope == "tenant", kill_switches.c.channel == "call")
        )
    ).scalar_one_or_none()
    return bool(engaged)


async def open_contacts(session: AsyncSession, campaign_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(campaign_contacts)
                .where(
                    campaign_contacts.c.campaign_id == campaign_id, campaign_contacts.c.state.in_(OPEN_CONTACT_STATES)
                )
            )
        ).scalar_one()
    )


async def complete_if_done(session: AsyncSession, campaign: dict[str, Any], actor: Actor) -> bool:
    """A running campaign whose audience snapshot is complete and has nothing left to do is completed."""
    if campaign["state"] != "running" or campaign["snapshot_status"] != "completed":
        return False
    if await open_contacts(session, campaign["id"]):
        return False
    await transition(
        session, campaign, "complete", actor=actor, values={"completed_at": func.now()}, reason="audience_done"
    )
    return True

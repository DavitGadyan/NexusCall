"""Call aggregate operations. Every state change goes through `transition`, which:

* locks the row and re-checks the legal transition (duplicate/out-of-order inputs become no-ops),
* bumps `version` with an optimistic check,
* writes `call.state_changed` (and `call.completed` for terminal states) to the outbox in the same transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import Integer, cast, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from call_service.deps import PRODUCER
from call_service.state_machine import ANSWERED, TERMINAL, can_transition, final_state
from call_service.tables import call_directory, calls, outbox
from nexus_core.audit import emit_audit
from nexus_core.context import Actor, current_correlation_id
from nexus_core.errors import Conflict, NotFound
from nexus_core.messaging import enqueue
from nexus_core.ratelimit import release_slot
from nexus_core.realtime import publish_ui_event
from nexus_events import ActorRef, make_event

SERVICE_ACTOR = Actor("service", PRODUCER)


def slot_key(tenant_id: uuid.UUID | str) -> str:
    return f"calls:slots:{tenant_id}"


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


async def load_call(session: AsyncSession, call_id: uuid.UUID, *, lock: bool = False) -> dict[str, Any]:
    stmt = select(calls).where(calls.c.id == call_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).mappings().first()
    if row is None:
        raise NotFound("Call not found")
    return dict(row)


async def find_by_provider_id(session: AsyncSession, provider_call_id: str) -> dict[str, Any] | None:
    row = (
        (await session.execute(select(calls).where(calls.c.provider_call_id == provider_call_id).with_for_update()))
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def set_check(session: AsyncSession, call_id: uuid.UUID, at: datetime | None, reason: str | None = None) -> None:
    """Schedule (or clear, with `at=None`) the next sweeper/reconciliation look at this call."""
    await session.execute(
        update(call_directory)
        .where(call_directory.c.call_id == call_id)
        .values(next_check_at=at, check_reason=reason if at is not None else None)
    )


async def register_directory(
    session: AsyncSession,
    *,
    call_id: uuid.UUID,
    tenant_id: uuid.UUID,
    provider_call_id: str | None = None,
    next_check_at: datetime | None = None,
    reason: str | None = None,
) -> None:
    await session.execute(
        insert(call_directory)
        .values(
            call_id=call_id,
            tenant_id=tenant_id,
            provider_call_id=provider_call_id,
            next_check_at=next_check_at,
            check_reason=reason if next_check_at else None,
        )
        .on_conflict_do_nothing()
    )


async def set_provider_call_id(session: AsyncSession, call: dict[str, Any], provider_call_id: str) -> dict[str, Any]:
    if call["provider_call_id"] == provider_call_id:
        return call
    if call["provider_call_id"] is not None:
        raise Conflict("Call already has a different provider call id")
    row = (
        (
            await session.execute(
                update(calls)
                .where(calls.c.id == call["id"])
                .values(provider_call_id=provider_call_id, updated_at=func.now())
                .returning(calls)
            )
        )
        .mappings()
        .one()
    )
    await session.execute(
        update(call_directory).where(call_directory.c.call_id == call["id"]).values(provider_call_id=provider_call_id)
    )
    return dict(row)


async def transition(
    session: AsyncSession,
    call: dict[str, Any],
    target: str,
    *,
    actor: Actor = SERVICE_ACTOR,
    end_reason: str | None = None,
    now: datetime | None = None,
    duration_seconds: int | None = None,
    **fields: Any,
) -> dict[str, Any] | None:
    """Apply `call.state -> target` if legal. Returns the updated row, or None when the change is not legal
    (duplicates and stale/out-of-order inputs). `call` must have been loaded with `lock=True`."""
    current = call["state"]
    if target == "completed" and call["opted_out"]:
        target = "opted_out"
    if not can_transition(current, target):
        return None
    values: dict[str, Any] = {"state": target, "version": calls.c.version + 1, "updated_at": func.now(), **fields}
    ts = now or func.now()
    if target == "initiating" and call["initiated_at"] is None:
        values["initiated_at"] = ts
    if target in ANSWERED and call["answered_at"] is None:
        values["answered_at"] = ts
    if target == "in_progress" and call["started_at"] is None:
        values["started_at"] = ts
    if target in TERMINAL:
        values["ended_at"] = ts
        values["end_reason"] = call["end_reason"] or end_reason or target
        if duration_seconds is not None:
            values["duration_seconds"] = duration_seconds
        elif call["answered_at"] is not None:
            values["duration_seconds"] = func.greatest(
                0, cast(func.extract("epoch", func.now() - calls.c.answered_at), Integer)
            )
        else:
            values["duration_seconds"] = 0
    elif end_reason is not None:
        values["blocked_reason"] = end_reason
    updated = (
        (
            await session.execute(
                update(calls)
                .where(calls.c.id == call["id"], calls.c.version == call["version"])
                .values(**values)
                .returning(calls)
            )
        )
        .mappings()
        .first()
    )
    if updated is None:  # someone changed it after we read it: the caller's view is stale
        raise Conflict("Call was changed concurrently; retry")
    row = dict(updated)
    tenant_id = row["tenant_id"]
    await emit(
        session,
        "call.state_changed",
        tenant_id=tenant_id,
        actor=actor,
        payload={
            "call_id": row["id"],
            "from_state": current,
            "to_state": target,
            "direction": row["direction"],
            "campaign_id": row["campaign_id"],
        },
    )
    if target in TERMINAL:
        await emit(
            session,
            "call.completed",
            tenant_id=tenant_id,
            actor=actor,
            payload={
                "call_id": row["id"],
                "direction": row["direction"],
                "final_state": final_state(target, row["end_reason"], opted_out=row["opted_out"]),
                "duration_seconds": int(row["duration_seconds"] or 0),
                "campaign_id": row["campaign_id"],
                "contact_id": row["contact_id"],
                "cost_micros": int(row["cost_micros"] or 0),
            },
        )
        await set_check(session, row["id"], None)
    return row


async def after_commit(redis: Redis | None, row: dict[str, Any] | None) -> None:
    """Best-effort side effects that must not run inside the transaction (slot release, UI hint)."""
    if row is None:
        return
    try:
        if row["state"] in TERMINAL:
            await release_slot(redis, slot_key(row["tenant_id"]), str(row["id"]))
        await publish_ui_event(
            redis,
            row["tenant_id"],
            "call.updated",
            permission="calls:read",
            data={"call_id": str(row["id"]), "state": row["state"]},
        )
    except Exception:  # noqa: BLE001 - UI hints and lease cleanup are best effort (leases expire on their own)
        return


def in_seconds(seconds: float, now: datetime) -> datetime:
    return now + timedelta(seconds=seconds)


def job_ctx(tenant_id: uuid.UUID, actor: Actor) -> Any:
    """JobContext for work enqueued from a request or event (correlation id carried over when it is a UUID)."""
    from nexus_core.celery_app import JobContext

    try:
        correlation = uuid.UUID(current_correlation_id())
    except ValueError:
        correlation = uuid.uuid4()
    return JobContext(tenant_id=tenant_id, correlation_id=correlation, actor_type=actor.type, actor_id=actor.id)

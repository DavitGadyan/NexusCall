"""Periodic jobs (ADR-0014): the sweeper and the reconciliation job.

Both read the ids-only `call_directory` work index (tenant rows are invisible without a tenant), then act on each
call inside that call's tenant session. They only select ids and enqueue work; provider calls happen in tasks.

* sweeper:        re-queues due `scheduled` calls, re-enqueues `queued` calls and unprocessed provider events
                  (a crash between commit and publish can never lose work).
* reconciliation: enqueues `calls.reconcile` for calls whose provider outcome is unknown or that went quiet.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from call_service.deps import Deps
from call_service.domain import SERVICE_ACTOR, after_commit, load_call, set_check, transition
from call_service.state_machine import LIVE, TERMINAL
from call_service.tables import call_directory, provider_events
from nexus_core.celery_app import JobContext
from nexus_core.context import current_correlation_id
from nexus_core.errors import NotFound

log = logging.getLogger(__name__)
BATCH = 200


async def _due(
    deps: Deps, reasons: tuple[str, ...], now: datetime, bump: timedelta
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    async with deps.db.global_session(SERVICE_ACTOR) as s:
        rows = (
            await s.execute(
                select(call_directory.c.call_id, call_directory.c.tenant_id)
                .where(call_directory.c.next_check_at <= now, call_directory.c.check_reason.in_(reasons))
                .order_by(call_directory.c.next_check_at)
                .limit(BATCH)
                .with_for_update(skip_locked=True)
            )
        ).all()
        if rows:
            # Push the next look out so a lost task is retried later, not in a tight loop.
            await s.execute(
                update(call_directory)
                .where(call_directory.c.call_id.in_([r.call_id for r in rows]))
                .values(next_check_at=now + bump)
            )
    return [(r.call_id, r.tenant_id) for r in rows]


def _ctx(tenant_id: uuid.UUID) -> JobContext:
    try:
        correlation = uuid.UUID(current_correlation_id())
    except ValueError:
        correlation = uuid.uuid4()
    return JobContext(
        tenant_id=tenant_id,
        correlation_id=correlation,
        actor_type="system",
        actor_id="call-sweeper",
    )


async def _pending_events(deps: Deps, ctx: JobContext, call_id: uuid.UUID) -> int:
    async with deps.db.tenant_session(ctx.tenant_id, ctx.actor) as s:
        ids = (
            await s.execute(
                select(provider_events.c.id).where(
                    provider_events.c.call_id == call_id, provider_events.c.status == "received"
                )
            )
        ).scalars()
        pending = list(ids)
    for event_id in pending:
        await deps.enqueue("calls.provider_callback", "calls.provider_callbacks", ctx.child(), event_id=str(event_id))
    return len(pending)


async def sweep(deps: Deps, *, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    handled = 0
    for call_id, tenant_id in await _due(deps, ("dispatch", "callbacks"), now, timedelta(seconds=60)):
        ctx = _ctx(tenant_id)
        try:
            handled += await _sweep_one(deps, ctx, call_id, now)
        except Exception:
            log.exception("call_sweep_failed", extra={"call_id": str(call_id)})
    return handled


async def _sweep_one(deps: Deps, ctx: JobContext, call_id: uuid.UUID, now: datetime) -> int:
    pending = await _pending_events(deps, ctx, call_id)
    row = None
    async with deps.db.tenant_session(ctx.tenant_id, ctx.actor) as s:
        try:
            call = await load_call(s, call_id, lock=True)
        except NotFound:
            await set_check(s, call_id, None)
            return 0
        state = call["state"]
        if state == "scheduled" and (call["scheduled_for"] is None or call["scheduled_for"] <= now):
            row = await transition(s, call, "queued", scheduled_for=None)
            state = "queued"
        if state == "queued":
            await set_check(s, call_id, now + timedelta(seconds=deps.settings.stale_queued_seconds), "dispatch")
        elif state == "scheduled":
            await set_check(s, call_id, call["scheduled_for"], "dispatch")
        elif state in TERMINAL and not pending:
            await set_check(s, call_id, None)
        elif state == "initiating":
            await set_check(s, call_id, now + timedelta(seconds=deps.settings.reconcile_after_seconds), "reconcile")
        elif state in LIVE and not pending:
            started = call["initiated_at"] or call["created_at"]
            await set_check(s, call_id, started + timedelta(seconds=deps.settings.live_call_max_seconds), "live")
    await after_commit(deps.redis, row)
    if state == "queued":
        await deps.enqueue("calls.dispatch", "calls.dispatch", ctx.child(), call_id=str(call_id))
    return 1


async def reconcile_due(deps: Deps, *, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    bump = timedelta(seconds=deps.settings.reconcile_after_seconds * 2)
    due = await _due(deps, ("reconcile", "live"), now, bump)
    for call_id, tenant_id in due:
        ctx = _ctx(tenant_id)
        try:
            await _pending_events(deps, ctx, call_id)
            await deps.enqueue("calls.reconcile", "calls.dispatch", ctx, call_id=str(call_id))
        except Exception:
            log.exception("call_reconcile_enqueue_failed", extra={"call_id": str(call_id)})
    return len(due)

"""Event consumers. The inbox drops duplicate deliveries; handlers are also idempotent on their own (state guards),
so calling one twice with the same event changes nothing the second time.

* campaign.paused / campaign.canceled / campaign.kill_switch -> cancel the campaign's (or tenant's) pending calls
* call.outcome.extracted (ai-agent-service)                  -> record the structured outcome of a call
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from call_service.domain import SERVICE_ACTOR, transition
from call_service.state_machine import PENDING
from call_service.tables import call_outcomes, calls
from nexus_core.messaging import Handler, PermanentEventError
from nexus_events import EventEnvelope
from nexus_events.payloads import CallOutcomeExtracted, CampaignRef, KillSwitch

log = logging.getLogger(__name__)

ALLOWED_PRODUCERS: dict[str, set[str]] = {
    "campaign.paused": {"campaign-service"},
    "campaign.canceled": {"campaign-service"},
    "campaign.kill_switch": {"campaign-service"},
    "call.outcome.extracted": {"ai-agent-service"},
}


def _tenant(event: EventEnvelope) -> uuid.UUID:
    if event.tenant_id is None:
        raise PermanentEventError("tenant-less event")
    return event.tenant_id


async def cancel_pending(session: AsyncSession, reason: str, campaign_id: uuid.UUID | None) -> int:
    """Cancel queued/scheduled outbound calls (all of them in the tenant when `campaign_id` is None)."""
    stmt = (
        select(calls)
        .where(calls.c.state.in_(sorted(PENDING)), calls.c.direction == "outbound")
        .with_for_update(skip_locked=False)
    )
    if campaign_id is not None:
        stmt = stmt.where(calls.c.campaign_id == campaign_id)
    canceled = 0
    for row in (await session.execute(stmt)).mappings().all():
        if await transition(session, dict(row), "canceled", end_reason=reason, actor=SERVICE_ACTOR) is not None:
            canceled += 1
    return canceled


async def on_campaign_stopped(event: EventEnvelope, session: AsyncSession) -> None:
    _tenant(event)
    ref = CampaignRef.model_validate(event.payload)
    if ref.channel != "call":
        return
    reason = "campaign_paused" if event.event_type == "campaign.paused" else "campaign_canceled"
    n = await cancel_pending(session, reason, ref.campaign_id)
    log.info("campaign_calls_canceled", extra={"campaign_id": str(ref.campaign_id), "count": n})


async def on_kill_switch(event: EventEnvelope, session: AsyncSession) -> None:
    _tenant(event)
    ks = KillSwitch.model_validate(event.payload)
    if not ks.engaged or ks.channel not in ("call", "all") or ks.scope == "platform":
        return
    campaign = ks.campaign_id if ks.scope == "campaign" else None
    await cancel_pending(session, "kill_switch", campaign)


async def on_outcome(event: EventEnvelope, session: AsyncSession) -> None:
    tenant_id = _tenant(event)
    p = CallOutcomeExtracted.model_validate(event.payload)
    exists = (await session.execute(select(calls.c.id).where(calls.c.id == p.call_id))).scalar_one_or_none()
    if exists is None:  # unknown in this tenant: never trust the event's ids blindly
        log.warning("outcome_unknown_call", extra={"call_id": str(p.call_id)})
        return
    await session.execute(
        insert(call_outcomes)
        .values(
            tenant_id=tenant_id,
            call_id=p.call_id,
            outcome=p.outcome[:64],
            qualified=p.qualified,
            source_event_id=event.event_id,
        )
        .on_conflict_do_update(
            constraint="uq_call_outcomes_call",
            set_={"outcome": p.outcome[:64], "qualified": p.qualified, "source_event_id": event.event_id},
        )
    )


HANDLERS: dict[str, Handler] = {
    "campaign.paused": on_campaign_stopped,
    "campaign.canceled": on_campaign_stopped,
    "campaign.kill_switch": on_kill_switch,
    "call.outcome.extracted": on_outcome,
}

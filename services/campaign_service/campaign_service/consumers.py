"""`call.completed` consumer: frees capacity, schedules retries, completes campaigns.

Duplicate deliveries are dropped by the inbox; a different event about an attempt that is no longer in flight
(or about an older call) is ignored by the state guard, so nothing is applied twice."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from campaign_service.domain import complete_if_done, record
from campaign_service.tables import call_campaigns, campaign_contacts
from nexus_core.context import Actor
from nexus_core.messaging import Handler, PermanentEventError
from nexus_events import EventEnvelope
from nexus_events.payloads import CallCompleted

log = logging.getLogger(__name__)
ACTOR = Actor("service", "campaign-service")
RETRYABLE = frozenset({"no_answer", "busy", "failed"})
ALLOWED_PRODUCERS: dict[str, set[str]] = {"call.completed": {"call-service"}}


def outcome_for(final_state: str, attempts: int, max_attempts: int) -> tuple[str, str | None, str | None]:
    """(contact state, outcome, skip reason) for a finished call."""
    if final_state == "completed":
        return "completed", "completed", None
    if final_state == "opted_out":
        return "skipped", "opted_out", "opted_out"
    if final_state == "canceled":
        return "skipped", "canceled", "call_canceled"
    if final_state in RETRYABLE:
        return ("retry_wait" if attempts < max_attempts else "failed"), final_state, None
    return "failed", final_state[:64], None


async def on_call_completed(event: EventEnvelope, session: AsyncSession) -> None:
    if event.tenant_id is None:
        raise PermanentEventError("tenant-less event")
    p = CallCompleted.model_validate(event.payload)
    if p.campaign_id is None:
        return  # inbound or test call
    campaign = (
        (await session.execute(select(call_campaigns).where(call_campaigns.c.id == p.campaign_id).with_for_update()))
        .mappings()
        .first()
    )
    if campaign is None:
        log.warning("call_completed_unknown_campaign", extra={"campaign_id": str(p.campaign_id)})
        return
    match_call = campaign_contacts.c.last_call_id == p.call_id
    if p.contact_id is not None:  # the call id may not be recorded yet if the call ended very quickly
        match_call = match_call | (
            (campaign_contacts.c.contact_id == p.contact_id) & campaign_contacts.c.last_call_id.is_(None)
        )
    cc = (
        (
            await session.execute(
                select(campaign_contacts)
                .where(campaign_contacts.c.campaign_id == p.campaign_id, match_call)
                .with_for_update()
            )
        )
        .mappings()
        .first()
    )
    if cc is None or cc["state"] != "in_flight":
        return  # stale or already applied
    state, outcome, skip_reason = outcome_for(p.final_state, cc["attempts"], campaign["max_attempts"])
    values: dict[str, Any] = {"state": state, "outcome": outcome, "skip_reason": skip_reason, "last_call_id": p.call_id}
    if state == "retry_wait":
        values["next_attempt_at"] = datetime.now(UTC) + timedelta(seconds=campaign["retry_delay_seconds"])
    else:
        values["completed_at"] = func.now()
    await session.execute(
        update(campaign_contacts)
        .where(campaign_contacts.c.id == cc["id"], campaign_contacts.c.state == "in_flight")
        .values(**values, version=campaign_contacts.c.version + 1, updated_at=func.now())
    )
    await record(
        session,
        tenant_id=event.tenant_id,
        campaign_id=p.campaign_id,
        kind="call_completed",
        actor=ACTOR,
        contact_id=cc["contact_id"],
        call_id=p.call_id,
        data={"final_state": p.final_state[:32], "contact_state": state, "attempt": int(cc["attempts"])},
    )
    await complete_if_done(session, dict(campaign), ACTOR)


HANDLERS: dict[str, Handler] = {"call.completed": on_call_completed}

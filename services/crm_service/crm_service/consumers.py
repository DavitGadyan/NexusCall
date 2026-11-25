"""Event consumers. The inbox (same transaction) dedupes redeliveries; each handler also re-validates every
referenced resource inside the event's tenant, so a forged or mismatched id simply finds nothing."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from crm_service.domain import add_consent, add_suppression, contact_updated, create_lead, latest_lead, load_contact
from crm_service.normalize import value_hash
from crm_service.tables import leads
from nexus_core.context import Actor
from nexus_core.errors import NotFound
from nexus_core.messaging import Handler, PermanentEventError
from nexus_events import EventEnvelope
from nexus_events.payloads import BookingRef, CallOptedOut, CallOutcomeExtracted, EmailAddressEvent

log = logging.getLogger(__name__)
ACTOR = Actor("service", "crm-service")
TERMINAL_LEAD = frozenset({"booked", "won", "lost"})

ALLOWED_PRODUCERS: dict[str, set[str]] = {
    "call.opted_out": {"call-service"},
    "email.unsubscribed": {"email-service"},
    "email.complaint": {"email-service"},
    "booking.created": {"booking-service"},
    "call.outcome.extracted": {"ai-agent-service"},
}


def _tenant(event: EventEnvelope) -> uuid.UUID:
    if event.tenant_id is None:
        raise PermanentEventError("tenant-less event")
    return event.tenant_id


async def _contact(session: AsyncSession, contact_id: uuid.UUID | None) -> dict[str, Any] | None:
    if contact_id is None:
        return None
    try:
        return await load_contact(session, contact_id, lock=True)
    except NotFound:
        log.warning("event_contact_not_found", extra={"contact_id": str(contact_id)})
        return None


async def on_call_opted_out(event: EventEnvelope, session: AsyncSession) -> None:
    tenant_id = _tenant(event)
    payload = CallOptedOut.model_validate(event.payload)
    contact = await _contact(session, payload.contact_id)
    if contact is None:
        return
    await add_suppression(
        session,
        tenant_id=tenant_id,
        channel="phone",
        reason="opt_out",
        source_type="call",
        source_id=str(payload.call_id),
        actor=ACTOR,
        contact_id=contact["id"],
    )
    await add_consent(
        session,
        tenant_id=tenant_id,
        contact_id=contact["id"],
        channel="phone",
        status="withdrawn",
        lawful_basis="consent",
        source="call_opt_out",
        source_ref=str(payload.call_id),
        actor=ACTOR,
        captured_at=event.occurred_at,
    )
    await contact_updated(session, tenant_id=tenant_id, actor=ACTOR, contact_id=contact["id"])


async def _email_event(event: EventEnvelope, session: AsyncSession, reason: str) -> None:
    tenant_id = _tenant(event)
    payload = EmailAddressEvent.model_validate(event.payload)
    contact = await _contact(session, payload.contact_id)
    # Link the contact only when its current address is the one the event is about.
    if contact is not None and (not contact["email"] or value_hash(contact["email"]) != payload.address_hash):
        contact = None
    await add_suppression(
        session,
        tenant_id=tenant_id,
        channel="email",
        reason=reason,
        source_type="email",
        source_id=str(payload.message_id) if payload.message_id else None,
        actor=ACTOR,
        contact_id=contact["id"] if contact else None,
        value_hash_override=payload.address_hash,
    )
    if contact is not None and reason == "opt_out":
        await add_consent(
            session,
            tenant_id=tenant_id,
            contact_id=contact["id"],
            channel="email",
            status="withdrawn",
            lawful_basis="consent",
            source="email_unsubscribe",
            source_ref=str(payload.message_id) if payload.message_id else None,
            actor=ACTOR,
            captured_at=event.occurred_at,
        )
    if contact is not None:
        await contact_updated(session, tenant_id=tenant_id, actor=ACTOR, contact_id=contact["id"])


async def on_email_unsubscribed(event: EventEnvelope, session: AsyncSession) -> None:
    await _email_event(event, session, "opt_out")


async def on_email_complaint(event: EventEnvelope, session: AsyncSession) -> None:
    await _email_event(event, session, "complaint")


async def _lead_for(
    session: AsyncSession, tenant_id: uuid.UUID, contact: dict[str, Any], source: str
) -> dict[str, Any]:
    lead = await latest_lead(session, contact["id"])
    if lead is None:
        lead = await create_lead(
            session, tenant_id=tenant_id, contact_id=contact["id"], actor=ACTOR, values={"source": source}
        )
    return lead


async def _update_lead(session: AsyncSession, lead_id: uuid.UUID, **values: Any) -> None:
    await session.execute(
        update(leads).where(leads.c.id == lead_id).values(**values, version=leads.c.version + 1, updated_at=func.now())
    )


async def on_booking_created(event: EventEnvelope, session: AsyncSession) -> None:
    tenant_id = _tenant(event)
    payload = BookingRef.model_validate(event.payload)
    contact = await _contact(session, payload.contact_id)
    if contact is None:
        return
    lead = await _lead_for(session, tenant_id, contact, "booking")
    values: dict[str, Any] = {"booking_id": payload.booking_id}
    if lead["status"] not in ("won", "lost"):
        values["status"] = "booked"
    await _update_lead(session, lead["id"], **values)


async def on_call_outcome(event: EventEnvelope, session: AsyncSession) -> None:
    tenant_id = _tenant(event)
    payload = CallOutcomeExtracted.model_validate(event.payload)
    contact = await _contact(session, payload.contact_id)
    if contact is None:
        return
    lead = await _lead_for(session, tenant_id, contact, "call")
    values: dict[str, Any] = {"call_id": payload.call_id, "last_outcome": payload.outcome[:64]}
    if payload.qualified is not None:
        values["qualified"] = payload.qualified
    if lead["status"] not in TERMINAL_LEAD:
        values["status"] = {True: "qualified", False: "unqualified", None: "contacted"}[payload.qualified]
    await _update_lead(session, lead["id"], **values)


HANDLERS: dict[str, Handler] = {
    "call.opted_out": on_call_opted_out,
    "email.unsubscribed": on_email_unsubscribed,
    "email.complaint": on_email_complaint,
    "booking.created": on_booking_created,
    "call.outcome.extracted": on_call_outcome,
}

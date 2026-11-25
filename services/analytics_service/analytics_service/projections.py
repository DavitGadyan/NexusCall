"""Event -> read-model projections (build.md §3.1O, §10.2 business metrics).

Idempotency without trusting delivery counts: a `daily_metrics` counter moves only when a fact row is
first inserted or a fact flag flips from false to true (`UPDATE ... WHERE NOT flag RETURNING`). A
redelivered or replayed event finds the fact already in that state and changes nothing. Facts tolerate
out-of-order events: the latest `state` wins by event time, and flags never flip back.

Payloads carry identifiers, enums and numbers only, so no personal data reaches these tables.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import Table, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from analytics_service.tables import (
    daily_metrics,
    fact_ai_usage,
    fact_bookings,
    fact_calls,
    fact_campaigns,
    fact_drafts,
    fact_emails,
    fact_leads,
)
from nexus_core.messaging import PermanentEventError
from nexus_events import EventEnvelope, payload_model_for

log = logging.getLogger(__name__)
Handler = Callable[[EventEnvelope, AsyncSession], Awaitable[None]]

ANSWERED_STATES = frozenset({"connected", "in_progress"})
EMAIL_FLAGS = {
    "accepted": "accepted",
    "delivered": "delivered",
    "bounced": "bounced",
    "complained": "complained",
    "rejected": "rejected",
    "failed": "failed",
    "unsubscribed": "unsubscribed",
}
# A later delivery state implies the provider accepted the message.
IMPLIES_ACCEPTED = frozenset({"delivered", "bounced", "complained", "unsubscribed"})

# Metrics exposed by the API (timeseries allowlist). Opens/clicks are intentionally absent.
METRICS = frozenset(
    {
        "calls.attempted",
        "calls.answered",
        "calls.ended",
        "calls.completed",
        "calls.failed",
        "calls.canceled",
        "calls.opted_out",
        "calls.qualified",
        "calls.duration_seconds",
        "calls.cost_micros",
        "bookings.created",
        "bookings.confirmed",
        "bookings.canceled",
        "emails.accepted",
        "emails.delivered",
        "emails.bounced",
        "emails.complained",
        "emails.rejected",
        "emails.failed",
        "emails.unsubscribed",
        "drafts.requested",
        "drafts.generated",
        "drafts.approved",
        "drafts.edited",
        "drafts.rejected",
        "drafts.sent",
        "ai.requests",
        "ai.input_tokens",
        "ai.output_tokens",
        "ai.cost_micros",
        "ai.latency_ms",
        "leads.created",
        "campaigns.launched",
    }
)


def _day(event: EventEnvelope) -> date:
    return event.occurred_at.astimezone(UTC).date()


def _dims(campaign_id: uuid.UUID | None, *extra: str) -> list[str]:
    dims = ["", *extra]
    if campaign_id is not None:
        dims.append(f"campaign:{campaign_id}")
    return dims


async def bump(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    day: date,
    metric: str,
    amount: int = 1,
    dimensions: list[str] | None = None,
) -> None:
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric}")
    if amount == 0:
        return
    for dimension in dimensions or [""]:
        stmt = insert(daily_metrics).values(
            tenant_id=tenant_id, day=day, metric=metric, dimension=dimension[:128], value=amount
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["tenant_id", "day", "metric", "dimension"],
                set_={"value": daily_metrics.c.value + stmt.excluded.value, "updated_at": func.now()},
            )
        )


async def _ensure(session: AsyncSession, table: Table, tenant_id: uuid.UUID, row_id: uuid.UUID, **values: Any) -> bool:
    """Insert the fact if it is new. True when this call created it."""
    created = (
        await session.execute(
            insert(table)
            .values(id=row_id, tenant_id=tenant_id, **values)
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(table.c.id)
        )
    ).scalar_one_or_none()
    return created is not None


async def _flip(session: AsyncSession, table: Table, row_id: uuid.UUID, flag: str, **values: Any) -> bool:
    """Set `flag` true once. True only for the call that flipped it."""
    flipped = (
        await session.execute(
            update(table)
            .where(table.c.id == row_id, table.c[flag].is_(False))
            .values({flag: True, "updated_at": func.now(), **values})
            .returning(table.c.id)
        )
    ).scalar_one_or_none()
    return flipped is not None


async def _latest_state(session: AsyncSession, table: Table, row_id: uuid.UUID, state: str, at: datetime) -> None:
    await session.execute(
        update(table)
        .where(table.c.id == row_id, or_(table.c.state_at.is_(None), table.c.state_at <= at))
        .values(state=state, state_at=at, updated_at=func.now())
    )


# --------------------------------------------------------------------------- calls
async def _call_fact(session: AsyncSession, ev: EventEnvelope, call_id: uuid.UUID, **values: Any) -> None:
    tid = ev.tenant_id
    assert tid is not None
    if await _ensure(session, fact_calls, tid, call_id, first_seen_at=ev.occurred_at, **values):
        await bump(session, tid, _day(ev), "calls.attempted", dimensions=_dims(values.get("campaign_id")))
    elif values.get("campaign_id"):
        await session.execute(
            update(fact_calls)
            .where(fact_calls.c.id == call_id, fact_calls.c.campaign_id.is_(None))
            .values(campaign_id=values["campaign_id"])
        )


async def _campaign_of(session: AsyncSession, call_id: uuid.UUID) -> uuid.UUID | None:
    return (await session.execute(select(fact_calls.c.campaign_id).where(fact_calls.c.id == call_id))).scalar()


async def on_call_state_changed(ev: EventEnvelope, s: AsyncSession, p: Any) -> None:
    await _call_fact(s, ev, p.call_id, campaign_id=p.campaign_id, direction=p.direction)
    await _latest_state(s, fact_calls, p.call_id, p.to_state, ev.occurred_at)
    if p.to_state in ANSWERED_STATES and await _flip(s, fact_calls, p.call_id, "answered"):
        await bump(s, ev.tenant_id, _day(ev), "calls.answered", dimensions=_dims(await _campaign_of(s, p.call_id)))  # type: ignore[arg-type]


async def on_call_completed(ev: EventEnvelope, s: AsyncSession, p: Any) -> None:
    tid = ev.tenant_id
    await _call_fact(s, ev, p.call_id, campaign_id=p.campaign_id, direction=p.direction, contact_id=p.contact_id)
    done = await _flip(
        s,
        fact_calls,
        p.call_id,
        "completed",
        final_state=p.final_state,
        duration_seconds=max(p.duration_seconds, 0),
        cost_micros=max(p.cost_micros, 0),
        completed_at=ev.occurred_at,
    )
    if not done:
        return
    dims = _dims(p.campaign_id or await _campaign_of(s, p.call_id))
    day = _day(ev)
    await bump(s, tid, day, "calls.ended", dimensions=dims)  # type: ignore[arg-type]
    if p.final_state in ("completed", "failed", "canceled"):
        await bump(s, tid, day, f"calls.{p.final_state}", dimensions=dims)  # type: ignore[arg-type]
    await bump(s, tid, day, "calls.duration_seconds", max(p.duration_seconds, 0), dims)  # type: ignore[arg-type]
    await bump(s, tid, day, "calls.cost_micros", max(p.cost_micros, 0), dims)  # type: ignore[arg-type]
    # A completed call with talk time was answered even if the state event was missed.
    if p.final_state == "completed" and p.duration_seconds > 0 and await _flip(s, fact_calls, p.call_id, "answered"):
        await bump(s, tid, day, "calls.answered", dimensions=dims)  # type: ignore[arg-type]


async def on_call_opted_out(ev: EventEnvelope, s: AsyncSession, p: Any) -> None:
    await _call_fact(s, ev, p.call_id, contact_id=p.contact_id)
    if await _flip(s, fact_calls, p.call_id, "opted_out"):
        await bump(s, ev.tenant_id, _day(ev), "calls.opted_out", dimensions=_dims(await _campaign_of(s, p.call_id)))  # type: ignore[arg-type]


async def on_call_outcome(ev: EventEnvelope, s: AsyncSession, p: Any) -> None:
    await _call_fact(s, ev, p.call_id, contact_id=p.contact_id)
    first = (
        await s.execute(
            update(fact_calls)
            .where(fact_calls.c.id == p.call_id, fact_calls.c.outcome.is_(None))
            .values(outcome=p.outcome[:64], qualified=p.qualified, updated_at=func.now())
            .returning(fact_calls.c.campaign_id)
        )
    ).first()
    if first is not None and p.qualified:
        await bump(s, ev.tenant_id, _day(ev), "calls.qualified", dimensions=_dims(first.campaign_id))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- bookings
async def on_booking(ev: EventEnvelope, s: AsyncSession, p: Any) -> None:
    tid, day = ev.tenant_id, _day(ev)
    assert tid is not None
    if await _ensure(s, fact_bookings, tid, p.booking_id, contact_id=p.contact_id, first_seen_at=ev.occurred_at):
        await bump(s, tid, day, "bookings.created")
    await s.execute(
        update(fact_bookings)
        .where(fact_bookings.c.id == p.booking_id)
        .values(status=p.status[:16], updated_at=func.now())
    )
    if ev.event_type == "booking.confirmed" and await _flip(s, fact_bookings, p.booking_id, "confirmed"):
        await bump(s, tid, day, "bookings.confirmed")
    if ev.event_type == "booking.canceled" and await _flip(s, fact_bookings, p.booking_id, "canceled"):
        await bump(s, tid, day, "bookings.canceled")


# --------------------------------------------------------------------------- emails
async def _email_flag(ev: EventEnvelope, s: AsyncSession, message_id: uuid.UUID, flag: str, campaign_id: Any,
                      category: str | None) -> None:  # fmt: skip
    if await _flip(s, fact_emails, message_id, flag):
        extra = (f"category:{category}",) if category else ()
        await bump(s, ev.tenant_id, _day(ev), f"emails.{flag}", dimensions=_dims(campaign_id, *extra))  # type: ignore[arg-type]


async def on_email_state(ev: EventEnvelope, s: AsyncSession, p: Any) -> None:
    tid = ev.tenant_id
    assert tid is not None
    await _ensure(
        s, fact_emails, tid, p.message_id, campaign_id=p.campaign_id, category=p.category, first_seen_at=ev.occurred_at
    )
    await _latest_state(s, fact_emails, p.message_id, p.state[:32], ev.occurred_at)
    if p.state in IMPLIES_ACCEPTED or p.state == "accepted":
        await _email_flag(ev, s, p.message_id, "accepted", p.campaign_id, p.category)
    flag = EMAIL_FLAGS.get(p.state)
    if flag and flag != "accepted":
        await _email_flag(ev, s, p.message_id, flag, p.campaign_id, p.category)


async def on_email_address_event(ev: EventEnvelope, s: AsyncSession, p: Any) -> None:
    tid = ev.tenant_id
    assert tid is not None
    flag = "unsubscribed" if ev.event_type == "email.unsubscribed" else "complained"
    if p.message_id is None:
        # No message to anchor on: counted once per event (the consumer inbox dedupes redelivery).
        await bump(s, tid, _day(ev), f"emails.{flag}")
        return
    await _ensure(s, fact_emails, tid, p.message_id, first_seen_at=ev.occurred_at)
    await _email_flag(ev, s, p.message_id, flag, None, None)


# --------------------------------------------------------------------------- AI drafts
async def _draft_fact(ev: EventEnvelope, s: AsyncSession, p: Any) -> None:
    assert ev.tenant_id is not None
    await _ensure(s, fact_drafts, ev.tenant_id, p.draft_id, thread_id=p.thread_id, first_seen_at=ev.occurred_at)


async def on_draft_requested(ev: EventEnvelope, s: AsyncSession, p: Any) -> None:
    # Keyed by the inbound message (no draft exists yet); the consumer inbox dedupes redelivery.
    await bump(s, ev.tenant_id, _day(ev), "drafts.requested")  # type: ignore[arg-type]


async def on_draft_generated(ev: EventEnvelope, s: AsyncSession, p: Any) -> None:
    await _draft_fact(ev, s, p)
    if await _flip(s, fact_drafts, p.draft_id, "generated", generated_version=p.version, status="generated"):
        await bump(s, ev.tenant_id, _day(ev), "drafts.generated")  # type: ignore[arg-type]


async def on_draft_approved(ev: EventEnvelope, s: AsyncSession, p: Any) -> None:
    await _draft_fact(ev, s, p)
    row = (
        await s.execute(
            update(fact_drafts)
            .where(fact_drafts.c.id == p.draft_id, fact_drafts.c.approved.is_(False))
            .values(approved=True, approved_version=p.version, status="approved", updated_at=func.now())
            .returning(fact_drafts.c.generated_version)
        )
    ).first()
    if row is None:
        return
    day = _day(ev)
    await bump(s, ev.tenant_id, day, "drafts.approved")  # type: ignore[arg-type]
    # Draft versions increase with every human edit: approving a later version than the generated one
    # means the reviewer edited it.
    generated = row.generated_version or 1
    if p.version is not None and p.version > generated and await _flip(s, fact_drafts, p.draft_id, "edited"):
        await bump(s, ev.tenant_id, day, "drafts.edited")  # type: ignore[arg-type]


async def on_draft_rejected(ev: EventEnvelope, s: AsyncSession, p: Any) -> None:
    await _draft_fact(ev, s, p)
    if await _flip(s, fact_drafts, p.draft_id, "rejected", status="rejected"):
        await bump(s, ev.tenant_id, _day(ev), "drafts.rejected")  # type: ignore[arg-type]


async def on_reply_sent(ev: EventEnvelope, s: AsyncSession, p: Any) -> None:
    await _draft_fact(ev, s, p)
    if await _flip(s, fact_drafts, p.draft_id, "sent", status="sent"):
        await bump(s, ev.tenant_id, _day(ev), "drafts.sent")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- AI usage, leads, campaigns
async def on_ai_request(ev: EventEnvelope, s: AsyncSession, p: Any) -> None:
    tid = ev.tenant_id
    assert tid is not None
    created = await _ensure(
        s,
        fact_ai_usage,
        tid,
        p.request_id,
        model=p.model[:64],
        provider=p.provider[:32],
        purpose=p.purpose[:32],
        input_tokens=max(p.input_tokens, 0),
        output_tokens=max(p.output_tokens, 0),
        cost_micros=max(p.cost_micros, 0),
        latency_ms=max(p.latency_ms, 0),
        occurred_at=ev.occurred_at,
    )
    if not created:
        return
    dims, day = ["", f"purpose:{p.purpose[:32]}"], _day(ev)
    await bump(s, tid, day, "ai.requests", 1, dims)
    await bump(s, tid, day, "ai.input_tokens", max(p.input_tokens, 0), dims)
    await bump(s, tid, day, "ai.output_tokens", max(p.output_tokens, 0), dims)
    await bump(s, tid, day, "ai.cost_micros", max(p.cost_micros, 0), dims)
    await bump(s, tid, day, "ai.latency_ms", max(p.latency_ms, 0), dims)


async def on_lead_created(ev: EventEnvelope, s: AsyncSession, p: Any) -> None:
    assert ev.tenant_id is not None
    if await _ensure(s, fact_leads, ev.tenant_id, p.contact_id, lead_id=p.lead_id, occurred_at=ev.occurred_at):
        await bump(s, ev.tenant_id, _day(ev), "leads.created")


async def on_campaign_launched(ev: EventEnvelope, s: AsyncSession, p: Any) -> None:
    assert ev.tenant_id is not None
    if await _ensure(s, fact_campaigns, ev.tenant_id, p.campaign_id, channel=p.channel, launched_at=ev.occurred_at):
        await bump(s, ev.tenant_id, _day(ev), "campaigns.launched", dimensions=["", f"channel:{p.channel}"])


Projection = Callable[[EventEnvelope, AsyncSession, Any], Awaitable[None]]
PROJECTIONS: dict[str, Projection] = {
    "call.state_changed": on_call_state_changed,
    "call.completed": on_call_completed,
    "call.opted_out": on_call_opted_out,
    "call.outcome.extracted": on_call_outcome,
    "booking.created": on_booking,
    "booking.confirmed": on_booking,
    "booking.canceled": on_booking,
    "email.message.state_changed": on_email_state,
    "email.unsubscribed": on_email_address_event,
    "email.complaint": on_email_address_event,
    "email.reply_draft.requested": on_draft_requested,
    "email.reply_draft.generated": on_draft_generated,
    "email.reply_draft.approved": on_draft_approved,
    "email.reply_draft.rejected": on_draft_rejected,
    "email.reply.sent": on_reply_sent,
    "ai.request.completed": on_ai_request,
    "lead.created": on_lead_created,
    "campaign.launched": on_campaign_launched,
}

_INBOX = {"email-inbox-service"}
ALLOWED_PRODUCERS: dict[str, set[str]] = {
    "call.state_changed": {"call-service"},
    "call.completed": {"call-service"},
    "call.opted_out": {"call-service"},
    "call.outcome.extracted": {"ai-agent-service"},
    "booking.created": {"booking-service"},
    "booking.confirmed": {"booking-service"},
    "booking.canceled": {"booking-service"},
    "email.message.state_changed": {"email-service"},
    "email.unsubscribed": {"email-service"},
    "email.complaint": {"email-service"},
    "email.reply_draft.requested": _INBOX,
    "email.reply_draft.generated": _INBOX | {"ai-agent-service"},
    "email.reply_draft.approved": _INBOX,
    "email.reply_draft.rejected": _INBOX,
    "email.reply.sent": _INBOX,
    "ai.request.completed": {"ai-gateway"},
    "lead.created": {"crm-service"},
    "campaign.launched": {"campaign-service"},
}


def _payload(event: EventEnvelope) -> BaseModel:
    model = payload_model_for(event.event_type, event.event_version)
    if model is None:
        raise PermanentEventError(f"unsupported event version {event.event_type}.v{event.event_version}")
    try:
        return model.model_validate(event.payload)
    except ValidationError as exc:
        raise PermanentEventError(f"invalid {event.event_type} payload") from exc


async def handle(event: EventEnvelope, session: AsyncSession) -> None:
    if event.tenant_id is None:
        log.warning("analytics_event_without_tenant", extra={"event_type": event.event_type})
        return
    await PROJECTIONS[event.event_type](event, session, _payload(event))


EVENT_HANDLERS: dict[str, Handler] = {event_type: handle for event_type in PROJECTIONS}

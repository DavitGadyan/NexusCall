"""Consumer handlers called directly, with duplicates, out-of-order and forged events (real database)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from analytics_service.projections import ALLOWED_PRODUCERS, EVENT_HANDLERS, METRICS
from analytics_service.tables import daily_metrics, fact_calls, inbox
from nexus_core.context import Actor
from nexus_core.messaging import EventConsumer, PermanentEventError
from nexus_events import ActorRef, make_event

pytestmark = [pytest.mark.integration]
CONSUMER = Actor("service", "analytics_service")
T0 = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


def ev(event_type, payload, tenant, *, producer=None, at=None):
    producer = producer or sorted(ALLOWED_PRODUCERS[event_type])[0]
    e = make_event(
        event_type,
        producer=producer,
        actor=ActorRef(type="service", id=producer),
        correlation_id=uuid.uuid4(),
        tenant_id=tenant,
        payload=payload,
    )
    return e.model_copy(update={"occurred_at": at or T0})


def deliver(run_db, *events):
    async def go(db):
        for e in events:
            async with db.tenant_session(e.tenant_id, CONSUMER) as s:
                await EVENT_HANDLERS[e.event_type](e, s)

    run_db(go)


def metrics(run_db, tenant, dimension=""):
    async def go(db):
        async with db.tenant_session(tenant) as s:
            rows = (
                await s.execute(
                    select(daily_metrics.c.metric, daily_metrics.c.value).where(daily_metrics.c.dimension == dimension)
                )
            ).all()
        out: dict[str, int] = {}
        for metric, value in rows:
            out[metric] = out.get(metric, 0) + value
        return out

    return run_db(go)


def test_call_lifecycle_counts_each_transition_once_even_when_redelivered(run_db):
    tenant, call, campaign = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    events = [
        ev("call.state_changed", {"call_id": str(call), "from_state": None, "to_state": "initiating",
                                  "direction": "outbound", "campaign_id": str(campaign)}, tenant),
        ev("call.state_changed", {"call_id": str(call), "from_state": "ringing", "to_state": "connected",
                                  "direction": "outbound", "campaign_id": str(campaign)}, tenant,
           at=T0 + timedelta(seconds=20)),
        ev("call.completed", {"call_id": str(call), "direction": "outbound", "final_state": "completed",
                              "duration_seconds": 95, "campaign_id": str(campaign), "cost_micros": 31_000}, tenant,
           at=T0 + timedelta(minutes=2)),
        ev("call.outcome.extracted", {"call_id": str(call), "outcome": "interested", "qualified": True}, tenant),
        ev("call.opted_out", {"call_id": str(call), "contact_id": None}, tenant),
    ]  # fmt: skip
    deliver(run_db, *events, *events, *reversed(events))
    m = metrics(run_db, tenant)
    assert m == {
        "calls.attempted": 1,
        "calls.answered": 1,
        "calls.ended": 1,
        "calls.completed": 1,
        "calls.duration_seconds": 95,
        "calls.cost_micros": 31_000,
        "calls.qualified": 1,
        "calls.opted_out": 1,
    }
    assert metrics(run_db, tenant, f"campaign:{campaign}")["calls.answered"] == 1

    async def fact(db):
        async with db.tenant_session(tenant) as s:
            return (await s.execute(select(fact_calls).where(fact_calls.c.id == call))).mappings().one()

    row = run_db(fact)
    # The late "initiating" redelivery did not overwrite the newer state.
    assert row["state"] == "connected" and row["final_state"] == "completed" and row["outcome"] == "interested"


def test_completed_before_any_state_event_still_counts_attempt_and_answer(run_db):
    tenant, call = uuid.uuid4(), uuid.uuid4()
    deliver(
        run_db,
        ev("call.completed", {"call_id": str(call), "direction": "inbound", "final_state": "completed",
                              "duration_seconds": 30}, tenant),
        ev("call.state_changed", {"call_id": str(call), "from_state": "ringing", "to_state": "in_progress",
                                  "direction": "inbound"}, tenant),
    )  # fmt: skip
    m = metrics(run_db, tenant)
    assert m["calls.attempted"] == 1 and m["calls.answered"] == 1 and m["calls.ended"] == 1


def test_email_funnel_bookings_drafts_ai_leads_and_campaigns(run_db):
    tenant, msg, draft, thread = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    booking, request, lead, campaign = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    events = [
        # Delivery arrives before acceptance: acceptance is implied and counted once.
        ev("email.message.state_changed", {"message_id": str(msg), "state": "delivered", "category": "campaign",
                                           "campaign_id": str(campaign)}, tenant),
        ev("email.message.state_changed", {"message_id": str(msg), "state": "accepted", "category": "campaign",
                                           "campaign_id": str(campaign)}, tenant),
        ev("email.complaint", {"address_hash": "a" * 64, "message_id": str(msg)}, tenant),
        ev("email.unsubscribed", {"address_hash": "b" * 64}, tenant),
        ev("booking.created", {"booking_id": str(booking), "status": "tentative"}, tenant),
        ev("booking.confirmed", {"booking_id": str(booking), "status": "confirmed"}, tenant),
        ev("email.reply_draft.generated", {"draft_id": str(draft), "thread_id": str(thread), "version": 1}, tenant),
        ev("email.reply_draft.approved", {"draft_id": str(draft), "thread_id": str(thread), "version": 3}, tenant),
        ev("email.reply.sent", {"draft_id": str(draft), "thread_id": str(thread), "version": 3}, tenant),
        ev("ai.request.completed", {"request_id": str(request), "model": "claude-sonnet-5-5", "provider": "anthropic",
                                    "input_tokens": 1200, "output_tokens": 300, "cost_micros": 9_000,
                                    "latency_ms": 800, "purpose": "email_draft"}, tenant),
        ev("lead.created", {"contact_id": str(lead)}, tenant),
        ev("campaign.launched", {"campaign_id": str(campaign), "channel": "email"}, tenant),
    ]  # fmt: skip
    deliver(run_db, *events, *events)
    m = metrics(run_db, tenant)
    assert m["emails.accepted"] == 1 and m["emails.delivered"] == 1 and m["emails.complained"] == 1
    assert m["emails.unsubscribed"] == 2  # the message-less unsubscribe relies on inbox dedupe (called twice here)
    assert m["bookings.created"] == 1 and m["bookings.confirmed"] == 1
    assert m["drafts.generated"] == 1 and m["drafts.approved"] == 1 and m["drafts.edited"] == 1
    assert m["drafts.sent"] == 1 and "drafts.rejected" not in m
    assert m["ai.requests"] == 1 and m["ai.cost_micros"] == 9_000 and m["ai.input_tokens"] == 1200
    assert metrics(run_db, tenant, "purpose:email_draft")["ai.requests"] == 1
    assert m["leads.created"] == 1 and m["campaigns.launched"] == 1
    assert set(m) <= METRICS and not any("open" in k or "click" in k for k in METRICS)


def test_unedited_approval_is_not_counted_as_edited(run_db):
    tenant, draft, thread = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    deliver(
        run_db,
        ev("email.reply_draft.generated", {"draft_id": str(draft), "thread_id": str(thread), "version": 1}, tenant),
        ev("email.reply_draft.approved", {"draft_id": str(draft), "thread_id": str(thread), "version": 1}, tenant),
        ev("email.reply_draft.rejected", {"draft_id": str(uuid.uuid4()), "thread_id": str(thread)}, tenant),
    )
    m = metrics(run_db, tenant)
    assert m["drafts.approved"] == 1 and "drafts.edited" not in m and m["drafts.rejected"] == 1


def test_invalid_payloads_and_tenantless_events(run_db):
    tenant = uuid.uuid4()
    bad = ev("lead.created", {"contact_id": str(uuid.uuid4())}, tenant).model_copy(
        update={"payload": {"contact_id": "nope"}}
    )
    with pytest.raises(PermanentEventError):
        deliver(run_db, bad)
    platform_event = ev("campaign.launched", {"campaign_id": str(uuid.uuid4())}, None)

    async def go(db):
        async with db.global_session(CONSUMER) as s:
            await EVENT_HANDLERS["campaign.launched"](platform_event, s)  # ignored, no tenant

    run_db(go)


class FakeMessage:
    def __init__(self, event):
        self.body, self.headers, self.acked = event.model_dump_json().encode(), {}, False

    async def ack(self):
        self.acked = True

    async def reject(self, requeue=False):
        raise AssertionError("unexpected reject")


def test_forged_producers_are_ignored(run_db):
    tenant = uuid.uuid4()
    payload = {"call_id": str(uuid.uuid4()), "direction": "outbound", "final_state": "completed",
               "duration_seconds": 10, "cost_micros": 10**12}  # fmt: skip
    forged = ev("call.completed", payload, tenant, producer="crm-service")
    genuine = ev("call.completed", {**payload, "cost_micros": 5}, tenant)

    async def go(db):
        consumer = EventConsumer(
            consumer_name="analytics_service", db=db, inbox=inbox, amqp_url="amqp://unused",
            handlers=EVENT_HANDLERS, allowed_producers=ALLOWED_PRODUCERS,
        )  # fmt: skip
        for m in (FakeMessage(forged), FakeMessage(genuine), FakeMessage(genuine)):
            await consumer.handle_message(m)  # type: ignore[arg-type]

    run_db(go)
    assert metrics(run_db, tenant)["calls.cost_micros"] == 5
    assert set(ALLOWED_PRODUCERS) == set(EVENT_HANDLERS)

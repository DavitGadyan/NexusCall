"""LOCAL DEVELOPMENT showcase data (see `nexus_core.devshowcase`): a month of finished Acme calls (inbound and
campaign outbound) with provider attempts and AI-extracted outcomes.

Idempotent (fixed ids, ON CONFLICT DO NOTHING). Runs as `call_app`, so RLS applies. Every call is terminal and
the call directory has no work scheduled (`next_check_at` is NULL), so no sweeper or reconciliation job acts on
them. Numbers are in the reserved 555-01xx range.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.dialects.postgresql import insert

from call_service.tables import call_attempts, call_directory, call_outcomes, calls
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devshowcase import INBOUND_NUMBER_ID, MAIN_LINE, TENANT, VOICE_AGENT_ID, Scenario

SEED_ACTOR = Actor("system", "dev-showcase")


def provider_call_id(call_id: object) -> str:
    return f"CA{str(call_id).replace('-', '')}"  # Twilio-shaped call SID (fake)


async def seed(db: Database, sc: Scenario) -> None:
    async with db.tenant_session(TENANT.id, SEED_ACTOR) as s:
        for c in sc.calls:
            inbound = c.direction == "inbound"
            pcid = provider_call_id(c.id)
            await s.execute(
                insert(calls)
                .values(
                    id=c.id,
                    tenant_id=TENANT.id,
                    direction=c.direction,
                    state=c.state,
                    contact_id=c.contact.id,
                    agent_id=VOICE_AGENT_ID,
                    campaign_id=c.campaign.id if c.campaign else None,
                    campaign_contact_id=c.member.id if c.member else None,
                    inbound_number_id=INBOUND_NUMBER_ID,
                    from_e164=c.contact.phone if inbound else MAIN_LINE,
                    to_e164=MAIN_LINE if inbound else c.contact.phone,
                    provider="twilio",
                    provider_call_id=pcid,
                    attempt_count=1,
                    initiated_at=c.initiated_at,
                    answered_at=c.answered_at,
                    started_at=c.started_at,
                    ended_at=c.ended_at,
                    duration_seconds=c.duration,
                    end_reason=c.end_reason,
                    last_provider_status=c.provider_status,
                    last_provider_sequence=4 if c.answered_at else 3,
                    handoff_state=c.handoff_state,
                    cost_micros=c.cost_micros,
                    opted_out=c.opted_out,
                    created_by="service:campaign-service" if c.campaign else "service:call-service",
                    version=5 if c.answered_at else 4,
                    created_at=c.created_at,
                    updated_at=(c.ended_at or c.created_at) + timedelta(seconds=40 if c.has_session else 1),
                )
                .on_conflict_do_nothing()
            )
            await s.execute(
                insert(call_directory)
                .values(call_id=c.id, tenant_id=TENANT.id, provider_call_id=pcid, created_at=c.created_at)
                .on_conflict_do_nothing()
            )
            if not inbound:
                await s.execute(
                    insert(call_attempts)
                    .values(
                        id=c.id,  # one provider attempt per call; reusing the call id keeps it fixed
                        tenant_id=TENANT.id,
                        call_id=c.id,
                        attempt_no=1,
                        idempotency_token=f"call:{c.id}:1",
                        status="created",
                        provider_call_id=pcid,
                        created_at=c.initiated_at or c.created_at,
                        updated_at=c.started_at or c.created_at,
                    )
                    .on_conflict_do_nothing()
                )
            if c.outcome and c.has_session:
                at = (c.ended_at or c.created_at) + timedelta(seconds=35)
                await s.execute(
                    insert(call_outcomes)
                    .values(
                        id=c.id,
                        tenant_id=TENANT.id,
                        call_id=c.id,
                        outcome=c.outcome,
                        qualified=c.qualified,
                        source="ai-agent-service",
                        created_at=at,
                        updated_at=at,
                    )
                    .on_conflict_do_nothing()
                )

"""LOCAL DEVELOPMENT showcase data (see `nexus_core.devshowcase`): 160 more fictional Acme contacts with leads,
tags, segments, consent evidence and the suppressions their calls and emails produced.

Idempotent (fixed ids, ON CONFLICT DO NOTHING). Runs as the restricted `crm_app` role, so RLS applies.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.dialects.postgresql import insert

from crm_service.normalize import value_hash
from crm_service.tables import consent_records, contact_tags, contacts, leads, segments, suppressions, tags
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devshowcase import SEGMENTS, TAGS, TENANT, Scenario, sid

SEED_ACTOR = Actor("system", "dev-showcase")


async def seed(db: Database, sc: Scenario) -> None:
    by = str(SEED_ACTOR)
    async with db.tenant_session(TENANT.id, SEED_ACTOR) as s:
        first_seen = min(c.created_at for c in sc.contacts)
        for i, (name, color) in enumerate(TAGS.values(), start=1):
            await s.execute(
                insert(tags)
                .values(id=sid("tag", i), tenant_id=TENANT.id, name=name, color=color, created_at=first_seen,
                        updated_at=first_seen)  # fmt: skip
                .on_conflict_do_nothing()
            )
        tag_ids = {key: sid("tag", i) for i, key in enumerate(TAGS, start=1)}
        for n, name, description, tag in SEGMENTS.values():
            await s.execute(
                insert(segments)
                .values(
                    id=sid("segment", n),
                    tenant_id=TENANT.id,
                    name=name,
                    description=description,
                    rules={"op": "and", "rules": [
                        {"field": "status", "operator": "eq", "value": "active"},
                        {"field": "tag", "operator": "has", "value": TAGS[tag][0]},
                    ]},  # fmt: skip
                    created_by=by,
                    created_at=first_seen,
                    updated_at=first_seen,
                )
                .on_conflict_do_nothing()
            )
        for c in sc.contacts:
            updated = max(c.created_at, c.lead_updated_at or c.created_at)
            await s.execute(
                insert(contacts)
                .values(
                    id=c.id,
                    tenant_id=TENANT.id,
                    first_name=c.first,
                    last_name=c.last,
                    email=c.email,
                    phone_e164=c.phone,
                    timezone=TENANT.timezone,
                    country="US",
                    locale="en-US",
                    source=c.source,
                    created_by=by,
                    created_at=c.created_at,
                    updated_at=updated,
                )
                .on_conflict_do_nothing()
            )
            await s.execute(
                insert(leads)
                .values(
                    id=c.lead_id,
                    tenant_id=TENANT.id,
                    contact_id=c.id,
                    status=c.lead_status,
                    source=c.lead_source,
                    campaign_id=c.lead_campaign_id,
                    call_id=c.lead_call_id,
                    booking_id=c.lead_booking_id,
                    score=c.lead_score,
                    qualified=c.lead_qualified,
                    last_outcome=c.last_outcome,
                    created_by=by,
                    created_at=c.created_at,
                    updated_at=updated,
                )
                .on_conflict_do_nothing()
            )
            for k, tag in enumerate(c.tags):
                await s.execute(
                    insert(contact_tags)
                    .values(id=sid("contact_tag", c.n * 10 + k), tenant_id=TENANT.id, contact_id=c.id,
                            tag_id=tag_ids[tag], created_at=c.created_at)  # fmt: skip
                    .on_conflict_do_nothing()
                )
            basis = "consent" if c.new_lead else "contract"
            for k, channel in enumerate(("email", "phone")):
                await s.execute(
                    insert(consent_records)
                    .values(
                        id=sid("consent", c.n * 10 + k),
                        tenant_id=TENANT.id,
                        contact_id=c.id,
                        channel=channel,
                        status="granted",
                        lawful_basis=basis,
                        source=c.source,
                        captured_at=c.created_at,
                        policy_version="2026-01",
                        recorded_by=by,
                        created_at=c.created_at,
                    )
                    .on_conflict_do_nothing()
                )
            if c.phone_opt_out:
                at, call_id = c.phone_opt_out
                await s.execute(
                    insert(consent_records)
                    .values(id=sid("consent", c.n * 10 + 5), tenant_id=TENANT.id, contact_id=c.id, channel="phone",
                            status="withdrawn", lawful_basis=basis, source="call", source_ref=str(call_id),
                            captured_at=at, recorded_by="service:crm-service",
                            created_at=at + timedelta(seconds=2))  # fmt: skip
                    .on_conflict_do_nothing()
                )
                await s.execute(
                    insert(suppressions)
                    .values(id=sid("suppression", c.n * 10 + 1), tenant_id=TENANT.id, channel="phone",
                            value_hash=value_hash(c.phone), value=c.phone, contact_id=c.id, reason="opt_out",
                            source_type="call", source_id=str(call_id), created_by="service:crm-service",
                            created_at=at + timedelta(seconds=2))  # fmt: skip
                    .on_conflict_do_nothing()
                )
            if c.email_suppressed:
                reason, at, message_id = c.email_suppressed
                await s.execute(
                    insert(suppressions)
                    .values(id=sid("suppression", c.n * 10 + 2), tenant_id=TENANT.id, channel="email",
                            value_hash=value_hash(c.email), value=c.email, contact_id=c.id, reason=reason,
                            source_type="email", source_id=str(message_id), created_by="service:crm-service",
                            created_at=at + timedelta(seconds=30))  # fmt: skip
                    .on_conflict_do_nothing()
                )

"""LOCAL DEVELOPMENT showcase data (see `nexus_core.devshowcase`): two crew calendars next to the demo "Main
calendar" and a month of Acme appointments (past visits completed, this and next week confirmed, a few
canceled or no-shows), most booked by the AI voice agent during the showcase calls.

Idempotent (fixed ids, ON CONFLICT DO NOTHING). Runs as `booking_app`, so RLS applies. No appointment is
tentative (nothing for the hold-expiry job) and no resource has an external calendar (nothing to sync).
"""

from __future__ import annotations

from datetime import time

from sqlalchemy.dialects.postgresql import insert

from booking_service.tables import appointments, availability_rules, booking_tenants, resources
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devshowcase import RESOURCES, TENANT, Scenario, sid

SEED_ACTOR = Actor("system", "dev-showcase")


async def seed(db: Database, sc: Scenario) -> None:
    async with db.tenant_session(TENANT.id, SEED_ACTOR) as s:
        await s.execute(insert(booking_tenants).values(tenant_id=TENANT.id).on_conflict_do_nothing())
        for i, key in enumerate(("hvac", "plumbing"), start=1):
            rid, name, kind, open_h, close_h = RESOURCES[key]
            await s.execute(
                insert(resources)
                .values(id=rid, tenant_id=TENANT.id, name=name, kind=kind, timezone=TENANT.timezone)
                .on_conflict_do_nothing()
            )
            for weekday in range(5):
                await s.execute(
                    insert(availability_rules)
                    .values(
                        id=sid("booking_rule", i * 10 + weekday),
                        tenant_id=TENANT.id,
                        resource_id=rid,
                        weekday=weekday,
                        start_time=time(open_h, 0),
                        end_time=time(close_h, 0),
                        slot_minutes=60,
                        buffer_before_minutes=0,
                        buffer_after_minutes=15,
                        min_notice_minutes=120,
                    )
                    .on_conflict_do_nothing()
                )
        for b in sc.bookings:
            await s.execute(
                insert(appointments)
                .values(
                    id=b.id,
                    tenant_id=TENANT.id,
                    resource_id=RESOURCES[b.resource][0],
                    contact_id=b.contact.id,
                    start_at=b.start,
                    end_at=b.end,
                    status=b.status,
                    source_type=b.source_type,
                    source_id=b.source_id,
                    cancel_reason=b.cancel_reason,
                    confirmed_at=b.confirmed_at,
                    canceled_at=b.canceled_at,
                    created_by=b.created_by,
                    version={"confirmed": 1, "canceled": 2}.get(b.status, 2),
                    created_at=b.created_at,
                    updated_at=b.updated_at or b.created_at,
                )
                .on_conflict_do_nothing()
            )

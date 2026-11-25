"""LOCAL DEVELOPMENT demo data: one bookable resource per demo tenant with weekday 09:00–17:00 rules.

Idempotent (deterministic ids, ON CONFLICT DO NOTHING) and runs as the restricted app role under RLS."""

from __future__ import annotations

from datetime import time

from sqlalchemy.dialects.postgresql import insert

from booking_service.tables import availability_rules, booking_tenants, resources
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devseed import DEMO_TENANTS, demo_id

SEED_ACTOR = Actor("system", "seed")


async def seed(database: Database) -> None:
    for index, tenant in enumerate(DEMO_TENANTS):
        offset = index * 100
        resource_id = demo_id("booking_resource", offset + 1)
        async with database.tenant_session(tenant.id, SEED_ACTOR) as s:
            await s.execute(insert(booking_tenants).values(tenant_id=tenant.id).on_conflict_do_nothing())
            await s.execute(
                insert(resources)
                .values(
                    id=resource_id, tenant_id=tenant.id, name="Main calendar", kind="calendar", timezone=tenant.timezone
                )  # fmt: skip
                .on_conflict_do_nothing()
            )
            for weekday in range(5):  # Monday..Friday
                await s.execute(
                    insert(availability_rules)
                    .values(
                        id=demo_id("booking_rule", offset * 10 + weekday + 1),
                        tenant_id=tenant.id,
                        resource_id=resource_id,
                        weekday=weekday,
                        start_time=time(9, 0),
                        end_time=time(17, 0),
                        slot_minutes=30,
                        buffer_before_minutes=0,
                        buffer_after_minutes=0,
                        min_notice_minutes=60,
                    )
                    .on_conflict_do_nothing()
                )

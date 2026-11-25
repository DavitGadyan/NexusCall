"""LOCAL DEVELOPMENT demo data: one inbound/caller-ID number per demo tenant, routed to its demo voice agent.

Idempotent (fixed ids, ON CONFLICT DO NOTHING). Runs as the restricted `call_app` role, so RLS applies.
Numbers are in the reserved 555-01xx range.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert

from call_service.tables import inbound_numbers, number_directory
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devseed import ACME, GLOBEX, demo_id

SEED_ACTOR = Actor("system", "dev-seed")
# (tenant, agent demo id, number, human handoff line)
NUMBERS = (
    (ACME, 1, "+12125550100", "+12125550198"),
    (GLOBEX, 101, "+13125550100", "+13125550198"),
)


async def seed(db: Database) -> None:
    for tenant, agent_n, e164, handoff in NUMBERS:
        number_id = demo_id("inbound_number", agent_n)
        async with db.tenant_session(tenant.id, SEED_ACTOR) as s:
            await s.execute(
                insert(number_directory)
                .values(e164=e164, tenant_id=tenant.id, inbound_number_id=number_id, status="active")
                .on_conflict_do_nothing()
            )
            await s.execute(
                insert(inbound_numbers)
                .values(
                    id=number_id,
                    tenant_id=tenant.id,
                    e164=e164,
                    agent_id=demo_id("agent", agent_n),
                    label="Main line (demo)",
                    handoff_e164=handoff,
                )
                .on_conflict_do_nothing()
            )

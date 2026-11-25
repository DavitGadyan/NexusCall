"""LOCAL DEVELOPMENT demo data: tool grants for the demo agents (ids shared with ai-agent-service).

Voice agents get the call tools (booking.confirm stays approval-gated); email agents get knowledge
search only. Idempotent; runs as mcpreg_app under RLS."""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert

from mcp_registry.tables import agent_tool_grants
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devseed import DEMO_TENANTS, demo_id

VOICE_TOOLS = (
    "knowledge.search",
    "booking.find_slots",
    "crm.get_contact",
    "crm.add_note",
    "booking.create_tentative",
    "call.transfer_to_human",
    "booking.confirm",
)
EMAIL_TOOLS = ("knowledge.search",)
SEEDER = Actor("system", "seed")


async def seed(db: Database) -> None:
    for index, tenant in enumerate(DEMO_TENANTS):
        offset = index * 100
        plan = ((demo_id("agent", 1 + offset), VOICE_TOOLS), (demo_id("agent", 2 + offset), EMAIL_TOOLS))
        async with db.tenant_session(tenant.id, SEEDER) as s:
            for agent_id, tools in plan:
                for tool_id in tools:
                    await s.execute(
                        insert(agent_tool_grants)
                        .values(
                            tenant_id=tenant.id,
                            agent_id=agent_id,
                            tool_id=tool_id,
                            requires_approval=tool_id == "booking.confirm",
                            granted_by=str(SEEDER),
                        )
                        .on_conflict_do_nothing(constraint="uq_agent_tool_grants_tool")
                    )

"""LOCAL DEVELOPMENT demo data: a few audit records per demo tenant plus one platform record.

Idempotent: deterministic event ids, and `append` dedupes on event id. Runs as `audit_app` (RLS applies).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from audit_service.chain import AuditRecord, append
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devseed import DEMO_TENANTS, DEMO_USERS, demo_id

SEED_ACTOR = Actor("system", "dev-seed")
_ACTIONS = (
    ("invitation.created", "invitation", {"roles": ["analyst"]}),
    ("membership.updated", "membership", {"roles": ["campaign_manager"]}),
    ("settings.updated", "tenant_settings", {"section": "communication"}),
    ("campaign.launched", "campaign", {"channel": "call"}),
)


async def seed(db: Database) -> None:
    base = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    owners = {t.slug: next(u for u in DEMO_USERS if (t.slug, "tenant_owner") in u.tenant_roles) for t in DEMO_TENANTS}
    for ti, tenant in enumerate(DEMO_TENANTS):
        async with db.tenant_session(tenant.id, SEED_ACTOR) as s:
            for i, (action, resource_type, meta) in enumerate(_ACTIONS):
                await append(
                    s,
                    AuditRecord(
                        event_id=demo_id("audit-event", ti * 100 + i),
                        tenant_id=tenant.id,
                        occurred_at=base + timedelta(hours=i),
                        actor_type="user",
                        actor_id=str(owners[tenant.slug].id),
                        action=action,
                        resource_type=resource_type,
                        resource_id=str(demo_id(resource_type, ti * 100 + i)),
                        outcome="success",
                        request_id=None,
                        correlation_id=None,
                        causation_id=None,
                        producer="dev-seed",
                        metadata=meta,
                    ),
                )
    async with db.global_session(SEED_ACTOR) as s:
        await append(
            s,
            AuditRecord(
                event_id=demo_id("audit-event", 9_999),
                tenant_id=None,
                occurred_at=base,
                actor_type="system",
                actor_id="dev-seed",
                action="platform.settings.updated",
                resource_type="platform_settings",
                resource_id="security",
                outcome="success",
                request_id=None,
                correlation_id=None,
                causation_id=None,
                producer="dev-seed",
                metadata={"section": "security"},
            ),
        )

"""LOCAL DEVELOPMENT demo data: a few in-app notifications per demo tenant.

Idempotent through deterministic dedupe keys. Runs as `notify_app`, so row-level security applies.
"""

from __future__ import annotations

from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devseed import DEMO_TENANTS, DEMO_USERS, demo_id
from notification_service.domain import create_notification

SEED_ACTOR = Actor("system", "dev-seed")
_ROWS = (
    ("draft_ready", "AI reply draft ready for review", "A new AI reply draft is waiting for review.",
     "emails:drafts:review", "/inbox"),
    ("import_completed", "Lead import finished", "Imported 40 rows: 40 created, 0 updated, 0 failed.",
     "leads:create", "/crm/imports"),
    ("handoff_requested", "A caller asked for a human", "A live call needs a human agent.", "calls:manage",
     "/calls"),
)  # fmt: skip


async def seed(db: Database) -> None:
    for tenant in DEMO_TENANTS:
        owner = next(u for u in DEMO_USERS if (tenant.slug, "tenant_owner") in u.tenant_roles)
        async with db.tenant_session(tenant.id, SEED_ACTOR) as s:
            for i, (kind, title, body, permission, link) in enumerate(_ROWS):
                await create_notification(
                    s,
                    tenant_id=tenant.id,
                    permission=permission,
                    kind=kind,
                    title=title,
                    body=body,
                    link=link,
                    source="dev-seed",
                    dedupe_key=f"seed:{demo_id('notification', i)}",
                )
            await create_notification(
                s,
                tenant_id=tenant.id,
                user_id=owner.id,
                kind="generic",
                title="Welcome to NexusCall",
                body="Your workspace is ready. Invite your team from Team and access.",
                link="/admin/team",
                source="dev-seed",
                dedupe_key="seed:welcome",
            )

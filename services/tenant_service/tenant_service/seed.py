"""LOCAL DEVELOPMENT demo data: two tenants, the dev-realm users, their memberships and platform roles.

Idempotent. Runs as the restricted `tenant_app` role, so row-level security applies to the seed too.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devseed import DEMO_TENANTS, DEMO_USERS
from tenant_service.config_store import put_tenant_section, tenant_section
from tenant_service.domain import load_roles, upsert_membership
from tenant_service.settings_schema import TENANT_SECTIONS, defaults_for_tenant
from tenant_service.tables import platform_role_assignments, tenant_settings, tenants, users

SEED_ACTOR = Actor("system", "dev-seed")


async def seed(db: Database) -> None:
    by_slug = {t.slug: t for t in DEMO_TENANTS}
    async with db.global_session(SEED_ACTOR) as s:
        for u in DEMO_USERS:
            await s.execute(
                insert(users)
                .values(id=u.id, email=u.email, display_name=u.name)
                .on_conflict_do_update(index_elements=[users.c.id], set_={"email": u.email, "display_name": u.name})
            )
            if u.platform_role:
                await s.execute(
                    insert(platform_role_assignments)
                    .values(user_id=u.id, role_key=u.platform_role)
                    .on_conflict_do_nothing()
                )
        for t in DEMO_TENANTS:
            await s.execute(
                insert(tenants)
                .values(id=t.id, slug=t.slug, name=t.name)
                .on_conflict_do_update(index_elements=[tenants.c.id], set_={"name": t.name})
            )
    for t in DEMO_TENANTS:
        async with db.tenant_session(t.id, SEED_ACTOR) as s:
            has_settings = (await s.execute(select(tenant_settings.c.section).limit(1))).first() is not None
            if not has_settings:
                for section in TENANT_SECTIONS:
                    data = defaults_for_tenant(section, t.name)
                    if section == "profile":
                        data["timezone"] = t.timezone
                    current = await tenant_section(s, t.id, section)
                    await put_tenant_section(s, t.id, section, data, None, int(current["_version"]))
            for u in DEMO_USERS:
                role_keys = [role for slug, role in u.tenant_roles if by_slug[slug].id == t.id]
                if role_keys:
                    await upsert_membership(
                        s,
                        tenant_id=t.id,
                        user_id=u.id,
                        role_list=await load_roles(s, role_keys),
                        granted_by=None,
                        team_id=None,
                    )

"""LOCAL DEVELOPMENT showcase data (see `nexus_core.devshowcase.PLATFORM_TENANTS`): eight fictional agency
clients (dental clinics, solar, real estate, law, home services, insurance; one suspended) with their team
members, so the platform console shows a real customer base.

The member accounts exist only in tenant-service (they are not in the Keycloak dev realm, so nobody can sign in
as them). Emails use the reserved `.example` TLD. Idempotent (fixed ids, ON CONFLICT DO NOTHING; settings are
written only for a tenant that has none). Runs as the restricted `tenant_app` role, so RLS applies.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devseed import DEMO_USERS
from nexus_core.devshowcase import PLATFORM_TENANTS, Scenario, platform_user_email, platform_user_id, sid
from tenant_service.config_store import put_tenant_section, tenant_section
from tenant_service.domain import load_roles
from tenant_service.settings_schema import TENANT_SECTIONS, defaults_for_tenant
from tenant_service.tables import membership_directory, membership_roles, memberships, tenant_settings, tenants, users

SEED_ACTOR = Actor("system", "dev-showcase")
PLATFORM_OWNER = next(u for u in DEMO_USERS if u.platform_role == "platform_owner").id


async def seed(db: Database, sc: Scenario) -> None:
    async with db.global_session(SEED_ACTOR) as s:
        for t in PLATFORM_TENANTS:
            created = datetime.combine(sc.anchor - timedelta(days=t.created_days_ago), time(15, 20), UTC)
            await s.execute(
                insert(tenants)
                .values(
                    id=t.id,
                    slug=t.slug,
                    name=t.name,
                    status=t.status,
                    created_by=PLATFORM_OWNER,
                    status_reason="Trial ended; awaiting a signed contract." if t.status == "suspended" else None,
                    version=2 if t.status == "suspended" else 1,
                    created_at=created,
                    updated_at=created if t.status == "active" else datetime.combine(
                        sc.anchor - timedelta(days=9), time(16, 5), UTC),
                )  # fmt: skip
                .on_conflict_do_nothing()
            )
            for k, (first, last, _role) in enumerate(t.members):
                joined = created + timedelta(days=k * 3, hours=k)
                await s.execute(
                    insert(users)
                    .values(id=platform_user_id(t, k), email=platform_user_email(t, k), display_name=f"{first} {last}",
                            last_seen_at=datetime.combine(sc.anchor - timedelta(days=(t.n + k) % 6), time(14, 10), UTC)
                            if t.status == "active" else None, created_at=joined, updated_at=joined)  # fmt: skip
                    .on_conflict_do_nothing()
                )
    for t in PLATFORM_TENANTS:
        created = datetime.combine(sc.anchor - timedelta(days=t.created_days_ago), time(15, 20), UTC)
        async with db.tenant_session(t.id, SEED_ACTOR) as s:
            if (await s.execute(select(tenant_settings.c.section).limit(1))).first() is None:
                for section in TENANT_SECTIONS:
                    data = defaults_for_tenant(section, t.name)
                    if section == "profile":
                        data["timezone"] = t.timezone
                    current = await tenant_section(s, t.id, section)
                    await put_tenant_section(s, t.id, section, data, None, int(current["_version"]))
            owner = platform_user_id(t, 0)
            for k, (_first, _last, role) in enumerate(t.members):
                mid = sid("membership", t.n * 10 + k)
                joined = created + timedelta(days=k * 3, hours=k)
                user_id = platform_user_id(t, k)
                await s.execute(
                    insert(memberships)
                    .values(id=mid, tenant_id=t.id, user_id=user_id, invited_by=None if k == 0 else owner,
                            created_at=joined, updated_at=joined)  # fmt: skip
                    .on_conflict_do_nothing()
                )
                for r in await load_roles(s, [role]):
                    await s.execute(
                        insert(membership_roles)
                        .values(tenant_id=t.id, membership_id=mid, role_id=r.id, granted_by=None if k == 0 else owner,
                                created_at=joined)  # fmt: skip
                        .on_conflict_do_nothing()
                    )
                await s.execute(
                    insert(membership_directory)
                    .values(user_id=user_id, tenant_id=t.id, membership_id=mid, status="active")
                    .on_conflict_do_nothing()
                )

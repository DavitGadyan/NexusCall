"""Authorization resolution — the source of truth every service consults (build.md §3.1D)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_core.auth.jwt import Principal
from nexus_core.authz import PLATFORM_ROLES, ResolvedAccess
from nexus_core.context import Actor
from nexus_core.db import Database
from tenant_service.catalogue import SUPPORT_SCOPES
from tenant_service.tables import (
    membership_roles,
    memberships,
    platform_role_assignments,
    role_permissions,
    roles,
    support_grants,
    tenants,
    users,
)


async def tenant_permissions(session: AsyncSession, user_id: uuid.UUID) -> tuple[frozenset[str], frozenset[str]]:
    """Permissions + role keys for the user in the tenant bound to `session` (RLS-scoped)."""
    rows = (
        await session.execute(
            select(roles.c.key, role_permissions.c.permission_key)
            .select_from(
                memberships.join(
                    membership_roles,
                    and_(
                        membership_roles.c.membership_id == memberships.c.id,
                        membership_roles.c.tenant_id == memberships.c.tenant_id,
                    ),
                )
                .join(roles, roles.c.id == membership_roles.c.role_id)
                .outerjoin(role_permissions, role_permissions.c.role_id == roles.c.id)
            )
            .where(memberships.c.user_id == user_id, memberships.c.status == "active", roles.c.scope == "tenant")
        )
    ).all()
    return frozenset(r.permission_key for r in rows if r.permission_key), frozenset(r.key for r in rows)


async def active_support_permissions(session: AsyncSession, user_id: uuid.UUID) -> frozenset[str]:
    now = datetime.now(UTC)
    rows = (
        (
            await session.execute(
                select(support_grants.c.scope).where(
                    support_grants.c.requested_by == user_id,
                    support_grants.c.status == "active",
                    support_grants.c.expires_at > now,
                )
            )
        )
        .scalars()
        .all()
    )
    perms: set[str] = set()
    for scope in rows:
        perms |= SUPPORT_SCOPES.get(scope, frozenset())
    return frozenset(perms)


class LocalAuthzResolver:
    """Used inside tenant-service itself and behind /internal/v1/authz/*."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def _user_active(self, user_id: uuid.UUID) -> bool:
        async with self.db.global_session() as s:
            status = (await s.execute(select(users.c.status).where(users.c.id == user_id))).scalar_one_or_none()
        return status == "active"

    async def resolve_tenant(self, principal: Principal, tenant_id: uuid.UUID) -> ResolvedAccess:
        try:
            user_id = uuid.UUID(principal.sub)
        except ValueError:
            return ResolvedAccess(False, frozenset())
        if not await self._user_active(user_id):
            return ResolvedAccess(False, frozenset())
        async with self.db.global_session() as s:
            status = (await s.execute(select(tenants.c.status).where(tenants.c.id == tenant_id))).scalar_one_or_none()
        if status is None:
            return ResolvedAccess(False, frozenset())
        actor = Actor("user", principal.sub)
        async with self.db.tenant_session(tenant_id, actor) as s:
            perms, role_keys = await tenant_permissions(s, user_id)
            support = (
                await active_support_permissions(s, user_id) if principal.realm_roles & PLATFORM_ROLES else frozenset()
            )
        if not perms and not support:
            return ResolvedAccess(False, frozenset(), tenant_status=status)
        return ResolvedAccess(True, perms | support, role_keys | ({"support"} if support else set()), status)

    async def resolve_platform(self, principal: Principal) -> frozenset[str]:
        """Platform permissions require BOTH the IdP realm role in the token AND a DB assignment."""
        token_roles = principal.realm_roles & PLATFORM_ROLES
        if not token_roles:
            return frozenset()
        try:
            user_id = uuid.UUID(principal.sub)
        except ValueError:
            return frozenset()
        async with self.db.global_session() as s:
            rows = (
                (
                    await s.execute(
                        select(role_permissions.c.permission_key)
                        .select_from(
                            platform_role_assignments.join(roles, roles.c.key == platform_role_assignments.c.role_key)
                            .join(role_permissions, role_permissions.c.role_id == roles.c.id)
                            .join(users, users.c.id == platform_role_assignments.c.user_id)
                        )
                        .where(
                            platform_role_assignments.c.user_id == user_id,
                            platform_role_assignments.c.role_key.in_(token_roles),
                            users.c.status == "active",
                            roles.c.scope == "platform",
                        )
                    )
                )
                .scalars()
                .all()
            )
        return frozenset(rows)


async def bump_authz_epoch(redis: Redis | None, tenant_id: uuid.UUID) -> None:
    """Invalidate cached permissions for every service immediately after membership/role changes."""
    if redis is not None:
        await redis.incr(f"authz_epoch:{tenant_id}")

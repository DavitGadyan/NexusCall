"""Tenant membership and invitation domain logic (build.md §18.3)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_core.errors import Conflict, Forbidden, Unprocessable
from tenant_service.security import hash_token, new_invitation_token, normalize_email
from tenant_service.tables import (
    invitation_tokens,
    invitations,
    membership_directory,
    membership_roles,
    memberships,
    role_permissions,
    roles,
    teams,
    users,
)

OPEN_INVITATION = ("pending", "sent", "delivery_failed")
INVALID_INVITATION = "This invitation is invalid or has expired"


@dataclass(frozen=True, slots=True)
class RoleInfo:
    id: uuid.UUID
    key: str
    name: str
    requires_mfa: bool
    permissions: frozenset[str]


async def load_roles(session: AsyncSession, keys: list[str]) -> list[RoleInfo]:
    if not keys:
        raise Unprocessable("At least one role is required")
    rows = (
        await session.execute(
            select(roles.c.id, roles.c.key, roles.c.name, roles.c.requires_mfa, role_permissions.c.permission_key)
            .select_from(roles.outerjoin(role_permissions, role_permissions.c.role_id == roles.c.id))
            .where(roles.c.key.in_(keys), roles.c.scope == "tenant")
        )
    ).all()
    by_key: dict[str, dict] = {}  # type: ignore[type-arg]
    for r in rows:
        entry = by_key.setdefault(r.key, {"id": r.id, "name": r.name, "mfa": r.requires_mfa, "perms": set()})
        if r.permission_key:
            entry["perms"].add(r.permission_key)
    missing = set(keys) - set(by_key)
    if missing:
        raise Unprocessable(f"Unknown or non-tenant roles: {sorted(missing)}")
    return [RoleInfo(v["id"], k, v["name"], v["mfa"], frozenset(v["perms"])) for k, v in by_key.items()]


def check_grantable(
    granter_permissions: frozenset[str],
    granter_roles: frozenset[str],
    target: list[RoleInfo],
    *,
    platform_override: bool,
) -> None:
    """Privilege-escalation guard: a granter may only grant roles whose permissions they hold themselves,
    and only a tenant owner (or an authorized platform admin) may grant tenant_owner."""
    if platform_override:
        return
    if "tenant:users:manage" not in granter_permissions:
        raise Forbidden("Missing permission: tenant:users:manage")
    for role in target:
        if role.key == "tenant_owner" and "tenant_owner" not in granter_roles:
            raise Forbidden("Only a tenant owner can grant the tenant owner role")
        excess = role.permissions - granter_permissions
        if excess:
            raise Forbidden(f"Cannot grant role '{role.key}': it includes permissions you do not hold")


async def active_owner_count(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(
                select(func.count(func.distinct(memberships.c.id)))
                .select_from(
                    memberships.join(
                        membership_roles,
                        and_(
                            membership_roles.c.membership_id == memberships.c.id,
                            membership_roles.c.tenant_id == memberships.c.tenant_id,
                        ),
                    ).join(roles, roles.c.id == membership_roles.c.role_id)
                )
                .where(memberships.c.status == "active", roles.c.key == "tenant_owner")
            )
        ).scalar_one()
    )


async def membership_role_keys(session: AsyncSession, membership_id: uuid.UUID) -> set[str]:
    return set(
        (
            await session.execute(
                select(roles.c.key)
                .select_from(membership_roles.join(roles, roles.c.id == membership_roles.c.role_id))
                .where(membership_roles.c.membership_id == membership_id)
            )
        ).scalars()
    )


async def upsert_membership(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    role_list: list[RoleInfo],
    granted_by: uuid.UUID | None,
    team_id: uuid.UUID | None,
) -> uuid.UUID:
    membership_id = (
        await session.execute(
            insert(memberships)
            .values(tenant_id=tenant_id, user_id=user_id, invited_by=granted_by, team_id=team_id)
            .on_conflict_do_update(
                constraint="uq_memberships_user",
                set_={"status": "active", "updated_at": func.now(), "version": memberships.c.version + 1},
            )
            .returning(memberships.c.id)
        )
    ).scalar_one()
    for role in role_list:
        await session.execute(
            insert(membership_roles)
            .values(tenant_id=tenant_id, membership_id=membership_id, role_id=role.id, granted_by=granted_by)
            .on_conflict_do_nothing()
        )
    await session.execute(
        insert(membership_directory)
        .values(user_id=user_id, tenant_id=tenant_id, membership_id=membership_id, status="active")
        .on_conflict_do_update(index_elements=["user_id", "tenant_id"], set_={"status": "active"})
    )
    return membership_id


async def set_membership_roles(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    membership_id: uuid.UUID,
    role_list: list[RoleInfo],
    granted_by: uuid.UUID,
) -> None:
    await session.execute(delete(membership_roles).where(membership_roles.c.membership_id == membership_id))
    for role in role_list:
        await session.execute(
            insert(membership_roles).values(
                tenant_id=tenant_id, membership_id=membership_id, role_id=role.id, granted_by=granted_by
            )
        )
    await session.execute(
        update(memberships)
        .where(memberships.c.id == membership_id)
        .values(version=memberships.c.version + 1, updated_at=func.now())
    )


@dataclass(frozen=True, slots=True)
class NewInvitation:
    id: uuid.UUID
    raw_token: str
    email: str
    expires_at: datetime
    superseded: uuid.UUID | None


async def create_invitation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    email: str,
    role_list: list[RoleInfo],
    team_id: uuid.UUID | None,
    invited_by: uuid.UUID | None,
    ttl_hours: int,
) -> NewInvitation:
    email = normalize_email(email)
    if team_id is not None and (await session.execute(select(teams.c.id).where(teams.c.id == team_id))).first() is None:
        raise Unprocessable("Unknown team")
    # A user who is already an active member does not need an invitation.
    existing_member = (
        await session.execute(
            select(memberships.c.id)
            .select_from(memberships.join(users, users.c.id == memberships.c.user_id))
            .where(users.c.email == email, memberships.c.status == "active")
        )
    ).first()
    if existing_member:
        raise Conflict("This person is already a member of the workspace")
    raw, token_hash = new_invitation_token()
    new_id = uuid.uuid4()
    previous = (
        await session.execute(
            select(invitations.c.id)
            .where(invitations.c.email == email, invitations.c.status.in_(OPEN_INVITATION))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if previous is not None:  # resend/re-invite supersedes the old token (build.md §18.3)
        await session.execute(
            update(invitations)
            .where(invitations.c.id == previous)
            .values(status="revoked", revoked_at=func.now(), superseded_by=new_id, updated_at=func.now())
        )
        await session.execute(delete(invitation_tokens).where(invitation_tokens.c.invitation_id == previous))
    expires = datetime.now(UTC) + timedelta(hours=ttl_hours)
    await session.execute(
        insert(invitations).values(
            id=new_id,
            tenant_id=tenant_id,
            email=email,
            role_keys=sorted(r.key for r in role_list),
            team_id=team_id,
            token_hash=token_hash,
            status="pending",
            expires_at=expires,
            invited_by=invited_by,
        )
    )
    await session.execute(
        insert(invitation_tokens).values(token_hash=token_hash, tenant_id=tenant_id, invitation_id=new_id)
    )
    return NewInvitation(new_id, raw, email, expires, previous)


async def lookup_token(session: AsyncSession, raw_token: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    if not raw_token or len(raw_token) > 128:
        return None
    row = (
        await session.execute(
            select(invitation_tokens.c.tenant_id, invitation_tokens.c.invitation_id).where(
                invitation_tokens.c.token_hash == hash_token(raw_token)
            )
        )
    ).first()
    return (row.tenant_id, row.invitation_id) if row else None


async def load_open_invitation(session: AsyncSession, invitation_id: uuid.UUID, *, lock: bool = False) -> dict | None:  # type: ignore[type-arg]
    """Returns the open invitation, or None. Expired invitations are marked `expired` (the caller's
    transaction commits that change) and also return None, so callers respond uniformly."""
    stmt = select(invitations).where(invitations.c.id == invitation_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).mappings().first()
    if row is None or row["status"] not in OPEN_INVITATION:
        return None
    if row["expires_at"] <= datetime.now(UTC):
        await session.execute(
            update(invitations).where(invitations.c.id == invitation_id).values(status="expired", updated_at=func.now())
        )
        await session.execute(delete(invitation_tokens).where(invitation_tokens.c.invitation_id == invitation_id))
        return None
    return dict(row)

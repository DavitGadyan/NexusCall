"""Tenant team & access management (Tenant Admin Console)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert

from nexus_core.authz import AuthContext, require_permission
from nexus_core.errors import Conflict, Forbidden, NotFound, Unprocessable
from tenant_service.access import bump_authz_epoch
from tenant_service.catalogue import TENANT_PERMISSIONS
from tenant_service.deps import audit, db, redis
from tenant_service.domain import (
    active_owner_count,
    check_grantable,
    load_roles,
    membership_role_keys,
    set_membership_roles,
)
from tenant_service.tables import (
    membership_directory,
    membership_roles,
    memberships,
    role_permissions,
    roles,
    support_grants,
    teams,
    users,
)

router = APIRouter(prefix="/api/v1", tags=["team"])
ManageUsers = Annotated[AuthContext, Depends(require_permission("tenant:users:manage"))]


class MembershipPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_keys: list[str] | None = Field(default=None, min_length=1, max_length=10)
    status: Literal["active", "deactivated"] | None = None
    team_id: uuid.UUID | None = None
    expected_version: int


class TeamIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=120)


@router.get("/memberships")
async def list_memberships(request: Request, ctx: ManageUsers) -> dict[str, Any]:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = (
            (
                await s.execute(
                    select(
                        memberships.c.id,
                        memberships.c.status,
                        memberships.c.team_id,
                        memberships.c.version,
                        memberships.c.created_at,
                        users.c.id.label("user_id"),
                        users.c.email,
                        users.c.display_name,
                        users.c.last_seen_at,
                        func.array_remove(func.array_agg(roles.c.key), None).label("roles"),
                    )
                    .select_from(
                        memberships.join(users, users.c.id == memberships.c.user_id)
                        .outerjoin(
                            membership_roles,
                            and_(
                                membership_roles.c.membership_id == memberships.c.id,
                                membership_roles.c.tenant_id == memberships.c.tenant_id,
                            ),
                        )
                        .outerjoin(roles, roles.c.id == membership_roles.c.role_id)
                    )
                    .group_by(memberships.c.id, users.c.id)
                    .order_by(users.c.email)
                )
            )
            .mappings()
            .all()
        )
    return {
        "items": [
            {
                **dict(r),
                "id": str(r["id"]),
                "user_id": str(r["user_id"]),
                "team_id": str(r["team_id"]) if r["team_id"] else None,
            }
            for r in rows
        ]
    }


@router.patch("/memberships/{membership_id}")
async def patch_membership(
    membership_id: uuid.UUID, body: MembershipPatch, request: Request, ctx: ManageUsers
) -> dict[str, Any]:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        row = (
            (await s.execute(select(memberships).where(memberships.c.id == membership_id).with_for_update()))
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound("Membership not found")
        if str(row["user_id"]) == ctx.principal.sub:
            raise Forbidden("You cannot change your own access; ask another administrator")
        if row["version"] != body.expected_version:
            raise Conflict("Membership was changed by someone else; reload and retry")
        current_roles = await membership_role_keys(s, membership_id)
        # Changing anyone who holds roles above the caller's own is also escalation.
        held = await load_roles(s, sorted(current_roles)) if current_roles else []
        check_grantable(ctx.permissions, ctx.roles, held, platform_override=False)
        removing_owner = "tenant_owner" in current_roles and (
            body.status == "deactivated" or (body.role_keys is not None and "tenant_owner" not in body.role_keys)
        )
        if removing_owner and await active_owner_count(s) <= 1:
            raise Conflict("A workspace must keep at least one active owner")
        changes: dict[str, Any] = {}
        if body.role_keys is not None:
            target = await load_roles(s, body.role_keys)
            check_grantable(ctx.permissions, ctx.roles, target, platform_override=False)
            await set_membership_roles(
                s,
                tenant_id=tenant_id,
                membership_id=membership_id,
                role_list=target,
                granted_by=uuid.UUID(ctx.principal.sub),
            )
            changes["roles"] = sorted(body.role_keys)
        if body.status is not None and body.status != row["status"]:
            await s.execute(
                update(memberships)
                .where(memberships.c.id == membership_id)
                .values(status=body.status, version=memberships.c.version + 1, updated_at=func.now())
            )
            await s.execute(
                update(membership_directory)
                .where(membership_directory.c.membership_id == membership_id)
                .values(status=body.status)
            )
            changes["status"] = body.status
        if "team_id" in body.model_fields_set:
            await s.execute(
                update(memberships)
                .where(memberships.c.id == membership_id)
                .values(team_id=body.team_id, version=memberships.c.version + 1, updated_at=func.now())
            )
            changes["team_id"] = str(body.team_id) if body.team_id else None
        if not changes:
            raise Unprocessable("No changes requested")
        await audit(
            s,
            ctx.actor,
            "membership.updated",
            "membership",
            membership_id,
            tenant_id=tenant_id,
            before_roles=sorted(current_roles),
            **changes,
        )
    await bump_authz_epoch(redis(request), tenant_id)
    return {"id": str(membership_id), "changes": changes}


@router.get("/roles")
async def list_roles(
    request: Request, ctx: Annotated[AuthContext, Depends(require_permission("tenant:users:manage"))]
) -> dict[str, Any]:
    async with db(request).global_session(ctx.actor) as s:
        rows = (
            (
                await s.execute(
                    select(
                        roles.c.key,
                        roles.c.name,
                        roles.c.description,
                        roles.c.requires_mfa,
                        func.array_agg(role_permissions.c.permission_key).label("permissions"),
                    )
                    .select_from(roles.outerjoin(role_permissions, role_permissions.c.role_id == roles.c.id))
                    .where(roles.c.scope == "tenant")
                    .group_by(roles.c.id)
                    .order_by(roles.c.name)
                )
            )
            .mappings()
            .all()
        )
    out = []
    for r in rows:
        perms = frozenset(p for p in r["permissions"] if p)
        grantable = (perms <= ctx.permissions) and (r["key"] != "tenant_owner" or "tenant_owner" in ctx.roles)
        out.append({**dict(r), "permissions": sorted(perms), "grantable": grantable})
    return {"items": out}


@router.get("/permissions")
async def list_permissions(_: ManageUsers) -> dict[str, Any]:
    return {"items": [{"key": k, "description": v} for k, v in TENANT_PERMISSIONS.items()]}


@router.get("/teams")
async def list_teams(request: Request, ctx: ManageUsers) -> dict[str, Any]:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = (await s.execute(select(teams.c.id, teams.c.name).order_by(teams.c.name))).all()
    return {"items": [{"id": str(r.id), "name": r.name} for r in rows]}


@router.post("/teams", status_code=201)
async def create_team(body: TeamIn, request: Request, ctx: ManageUsers) -> dict[str, Any]:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        team_id = (
            await s.execute(
                insert(teams).values(tenant_id=tenant_id, name=body.name).on_conflict_do_nothing().returning(teams.c.id)
            )
        ).scalar_one_or_none()
        if team_id is None:
            raise Conflict("A team with this name already exists")
        await audit(s, ctx.actor, "team.created", "team", team_id, tenant_id=tenant_id, name=body.name)
    return {"id": str(team_id), "name": body.name}


@router.get("/support-grants")
async def tenant_support_grants(
    request: Request, ctx: Annotated[AuthContext, Depends(require_permission("audit:read"))]
) -> dict[str, Any]:
    """Tenants can always see who has (or requested) support access to their workspace."""
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = (
            (await s.execute(select(support_grants).order_by(support_grants.c.created_at.desc()).limit(100)))
            .mappings()
            .all()
        )
    return {"items": [{k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in r.items()} for r in rows]}

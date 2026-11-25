"""Platform Admin Console API (build.md §18.1A). Restricted to platform identities; every high-risk
action requires step-up MFA and is audited. No endpoint exposes passwords, tokens or secrets."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, EmailStr, Field, ValidationError
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from nexus_core.authz import AuthContext, require_permission, require_step_up
from nexus_core.context import Actor
from nexus_core.errors import Conflict, Forbidden, NotFound, Unprocessable
from nexus_core.messaging import enqueue
from nexus_events import ActorRef, make_event
from tenant_service.access import bump_authz_epoch
from tenant_service.api.invitations import InvitationIn, issue_and_deliver
from tenant_service.catalogue import PLATFORM_PERMISSIONS, ROLE_BY_KEY, SUPPORT_SCOPES, TENANT_PERMISSIONS
from tenant_service.config_store import all_platform, platform_section, put_platform_section, put_tenant_section
from tenant_service.deps import PRODUCER, audit, db, idp, redis
from tenant_service.settings_schema import (
    HIGH_RISK_PLATFORM_SECTIONS,
    PLATFORM_SECTIONS,
    TENANT_SECTIONS,
    defaults_for_tenant,
)
from tenant_service.tables import (
    feature_flags,
    invitations,
    membership_directory,
    outbox,
    platform_role_assignments,
    privileged_requests,
    rls_scans,
    role_permissions,
    roles,
    service_accounts,
    support_grants,
    tenants,
    users,
)

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])
log = logging.getLogger(__name__)


def perm(p: str) -> Any:
    return Annotated[AuthContext, Depends(require_permission(p))]


StepUp = Annotated[AuthContext, Depends(require_step_up())]


def _iso(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    return v


def _row(r: Any) -> dict[str, Any]:
    return {k: _iso(v) for k, v in dict(r).items()}


# ---------------------------------------------------------------- overview
@router.get("/overview")
async def overview(request: Request, ctx: perm("platform:overview:read")) -> dict[str, Any]:
    async with db(request).global_session(ctx.actor) as s:
        by_status = dict((await s.execute(select(tenants.c.status, func.count()).group_by(tenants.c.status))).all())
        user_count = (await s.execute(select(func.count()).select_from(users))).scalar_one()
        active_members = (
            await s.execute(
                select(func.count()).select_from(membership_directory).where(membership_directory.c.status == "active")
            )
        ).scalar_one()
        pending_priv = (
            await s.execute(
                select(func.count()).select_from(privileged_requests).where(privileged_requests.c.status == "pending")
            )
        ).scalar_one()
        last_scan = (
            await s.execute(
                select(rls_scans.c.created_at, rls_scans.c.summary).order_by(rls_scans.c.created_at.desc()).limit(1)
            )
        ).first()
        kill = await platform_section(s, "kill_switches")
        maintenance = await platform_section(s, "maintenance")
    return {
        "tenants": by_status,
        "users": user_count,
        "active_memberships": active_members,
        "pending_privileged_requests": pending_priv,
        "last_rls_scan": {"at": _iso(last_scan.created_at), "summary": last_scan.summary} if last_scan else None,
        "kill_switches": kill,
        "maintenance": maintenance,
    }


# ---------------------------------------------------------------- tenants
class TenantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=200)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    owner_email: EmailStr
    timezone: str = "UTC"
    data_region: str = Field("default", pattern=r"^[a-z0-9-]{2,32}$")


class TenantStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["active", "suspended", "archived"]
    reason: str = Field(min_length=5, max_length=500)


@router.get("/tenants")
async def list_tenants(
    request: Request, ctx: perm("platform:tenants:read"), q: str | None = None, status: str | None = None
) -> dict[str, Any]:
    async with db(request).global_session(ctx.actor) as s:
        members = (
            select(membership_directory.c.tenant_id, func.count().label("members"))
            .where(membership_directory.c.status == "active")
            .group_by(membership_directory.c.tenant_id)
            .subquery()
        )
        stmt = (
            select(tenants, func.coalesce(members.c.members, 0).label("members"))
            .select_from(tenants.outerjoin(members, members.c.tenant_id == tenants.c.id))
            .order_by(tenants.c.created_at.desc())
            .limit(500)
        )
        if q:
            stmt = stmt.where(or_(tenants.c.name.ilike(f"%{q}%"), tenants.c.slug.ilike(f"%{q}%")))
        if status:
            stmt = stmt.where(tenants.c.status == status)
        rows = (await s.execute(stmt)).mappings().all()
    return {"items": [_row(r) for r in rows]}


@router.post("/tenants", status_code=201)
async def create_tenant(
    body: TenantCreate, request: Request, ctx: perm("platform:tenants:manage"), _: StepUp
) -> dict[str, Any]:
    """Creates the workspace, default settings and the owner's invitation in one audited operation."""
    if body.timezone not in __import__("zoneinfo").available_timezones():
        raise Unprocessable("Unknown timezone")
    database = db(request)
    actor = ctx.actor
    async with database.global_session(actor) as s:
        tenant_id = (
            await s.execute(
                insert(tenants)
                .values(slug=body.slug, name=body.name, data_region=body.data_region, created_by=uuid.UUID(actor.id))
                .on_conflict_do_nothing(constraint="uq_tenants_slug")
                .returning(tenants.c.id)
            )
        ).scalar_one_or_none()
        if tenant_id is None:
            raise Conflict("A tenant with this slug already exists")
    async with database.tenant_session(tenant_id, actor) as s:
        for section in TENANT_SECTIONS:
            data = defaults_for_tenant(section, body.name)
            if section == "profile":
                data["timezone"] = body.timezone
            await put_tenant_section(s, tenant_id, section, data, uuid.UUID(actor.id), 0)
        await audit(s, actor, "tenant.created", "tenant", tenant_id, tenant_id=tenant_id, slug=body.slug)
        await enqueue(
            s,
            outbox,
            make_event(
                "tenant.created",
                producer=PRODUCER,
                actor=ActorRef(type="user", id=actor.id),
                correlation_id=uuid.uuid4(),
                tenant_id=tenant_id,
                payload={"tenant_id": str(tenant_id), "slug": body.slug},
            ),
        )
    owner = await issue_and_deliver(
        request,
        tenant_id=tenant_id,
        actor=actor,
        granter=None,
        platform_override=True,
        body=InvitationIn(email=body.owner_email, role_keys=["tenant_owner"]),
    )
    return {"id": str(tenant_id), "slug": body.slug, "name": body.name, "owner_invitation": owner}


@router.get("/tenants/{tenant_id}")
async def get_tenant(tenant_id: uuid.UUID, request: Request, ctx: perm("platform:tenants:read")) -> dict[str, Any]:
    database = db(request)
    async with database.global_session(ctx.actor) as s:
        row = (await s.execute(select(tenants).where(tenants.c.id == tenant_id))).mappings().first()
        if row is None:
            raise NotFound("Tenant not found")
        members = (
            (
                await s.execute(
                    select(users.c.id, users.c.email, users.c.display_name, membership_directory.c.status)
                    .select_from(membership_directory.join(users, users.c.id == membership_directory.c.user_id))
                    .where(membership_directory.c.tenant_id == tenant_id)
                    .order_by(users.c.email)
                )
            )
            .mappings()
            .all()
        )
    # Tenant-owned rows are read inside that tenant's RLS context, on behalf of the platform admin.
    async with database.tenant_session(tenant_id, ctx.actor) as s:
        open_invites = (
            await s.execute(
                select(func.count())
                .select_from(invitations)
                .where(invitations.c.status.in_(("pending", "sent", "delivery_failed")))
            )
        ).scalar_one()
        grants = (
            (await s.execute(select(support_grants).order_by(support_grants.c.created_at.desc()).limit(20)))
            .mappings()
            .all()
        )
    return {
        **_row(row),
        "members": [_row(m) for m in members],
        "open_invitations": open_invites,
        "support_grants": [_row(g) for g in grants],
    }


@router.post("/tenants/{tenant_id}/status")
async def set_tenant_status(
    tenant_id: uuid.UUID, body: TenantStatusIn, request: Request, ctx: perm("platform:tenants:manage"), _: StepUp
) -> dict[str, Any]:
    database = db(request)
    async with database.global_session(ctx.actor) as s:
        current = (
            await s.execute(select(tenants.c.status).where(tenants.c.id == tenant_id).with_for_update())
        ).scalar_one_or_none()
        if current is None:
            raise NotFound("Tenant not found")
        if current == "archived" and body.status != "archived":
            raise Conflict("Archived tenants cannot be reactivated through the UI")
        await s.execute(
            update(tenants)
            .where(tenants.c.id == tenant_id)
            .values(status=body.status, status_reason=body.reason, version=tenants.c.version + 1, updated_at=func.now())
        )
    async with database.tenant_session(tenant_id, ctx.actor) as s:
        await audit(
            s,
            ctx.actor,
            f"tenant.{body.status}",
            "tenant",
            tenant_id,
            tenant_id=tenant_id,
            previous=current,
            reason=body.reason,
        )
        await enqueue(
            s,
            outbox,
            make_event(
                "tenant.status_changed",
                producer=PRODUCER,
                actor=ActorRef(type="user", id=ctx.actor.id),
                correlation_id=uuid.uuid4(),
                tenant_id=tenant_id,
                payload={"tenant_id": str(tenant_id), "status": body.status},
            ),
        )
    await bump_authz_epoch(redis(request), tenant_id)
    return {"id": str(tenant_id), "status": body.status}


@router.post("/tenants/{tenant_id}/invitations", status_code=201)
async def platform_invite(
    tenant_id: uuid.UUID, body: InvitationIn, request: Request, ctx: perm("platform:tenants:manage"), _: StepUp
) -> dict[str, Any]:
    return await issue_and_deliver(
        request, tenant_id=tenant_id, body=body, actor=ctx.actor, granter=None, platform_override=True
    )


# ---------------------------------------------------------------- users
@router.get("/users")
async def list_users(request: Request, ctx: perm("platform:users:read"), q: str | None = None) -> dict[str, Any]:
    async with db(request).global_session(ctx.actor) as s:
        counts = (
            select(membership_directory.c.user_id, func.count().label("tenants"))
            .where(membership_directory.c.status == "active")
            .group_by(membership_directory.c.user_id)
            .subquery()
        )
        stmt = (
            select(
                users,
                func.coalesce(counts.c.tenants, 0).label("tenants"),
                func.array_remove(func.array_agg(platform_role_assignments.c.role_key), None).label("platform_roles"),
            )
            .select_from(
                users.outerjoin(counts, counts.c.user_id == users.c.id).outerjoin(
                    platform_role_assignments, platform_role_assignments.c.user_id == users.c.id
                )
            )
            .group_by(users.c.id, counts.c.tenants)
            .order_by(users.c.email)
            .limit(500)
        )
        if q:
            stmt = stmt.where(or_(users.c.email.ilike(f"%{q}%"), users.c.display_name.ilike(f"%{q}%")))
        rows = (await s.execute(stmt)).mappings().all()
    return {"items": [_row(r) for r in rows]}


@router.get("/users/{user_id}")
async def get_user(user_id: uuid.UUID, request: Request, ctx: perm("platform:users:read")) -> dict[str, Any]:
    async with db(request).global_session(ctx.actor) as s:
        row = (await s.execute(select(users).where(users.c.id == user_id))).mappings().first()
        if row is None:
            raise NotFound("User not found")
        tenants_rows = (
            (
                await s.execute(
                    select(
                        tenants.c.id,
                        tenants.c.name,
                        tenants.c.status,
                        membership_directory.c.status.label("membership"),
                    )
                    .select_from(membership_directory.join(tenants, tenants.c.id == membership_directory.c.tenant_id))
                    .where(membership_directory.c.user_id == user_id)
                )
            )
            .mappings()
            .all()
        )
        proles = (
            (
                await s.execute(
                    select(platform_role_assignments.c.role_key).where(platform_role_assignments.c.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
    idp_user, sessions = None, []
    try:
        idp_user = await idp(request).get_user(str(user_id))
        sessions = await idp(request).sessions(str(user_id))
    except Exception:  # noqa: BLE001 - an IdP outage must not break the platform view
        log.warning("idp_lookup_failed", extra={"user": str(user_id)})
    return {
        **_row(row),
        "memberships": [_row(t) for t in tenants_rows],
        "platform_roles": list(proles),
        "idp": None
        if idp_user is None
        else {
            "enabled": idp_user.enabled,
            "email_verified": idp_user.email_verified,
            "mfa_enrolled": idp_user.has_otp,
            "realm_roles": sorted(idp_user.realm_roles),
        },
        "sessions": [
            {"id": x.id, "ip": x.ip, "started": x.started, "last_access": x.last_access, "clients": x.clients}
            for x in sessions
        ],
    }


@router.get("/users/{user_id}/effective-permissions")
async def effective_permissions(
    user_id: uuid.UUID, tenant_id: uuid.UUID, request: Request, ctx: perm("platform:iam:read")
) -> dict[str, Any]:
    from tenant_service.access import tenant_permissions

    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        perms, role_keys = await tenant_permissions(s, user_id)
    return {
        "user_id": str(user_id),
        "tenant_id": str(tenant_id),
        "roles": sorted(role_keys),
        "permissions": sorted(perms),
    }


class UserAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=5, max_length=500)


@router.post("/users/{user_id}/deactivate")
async def deactivate_user(
    user_id: uuid.UUID, body: UserAction, request: Request, ctx: perm("platform:users:manage"), _: StepUp
) -> dict[str, Any]:
    if str(user_id) == ctx.principal.sub:
        raise Forbidden("You cannot deactivate yourself")
    async with db(request).global_session(ctx.actor) as s:
        r = await s.execute(
            update(users).where(users.c.id == user_id).values(status="deactivated", updated_at=func.now())
        )
        if r.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFound("User not found")
        tenant_ids = (
            (await s.execute(select(membership_directory.c.tenant_id).where(membership_directory.c.user_id == user_id)))
            .scalars()
            .all()
        )
        await audit(s, ctx.actor, "user.deactivated", "user", user_id, tenant_id=None, reason=body.reason)
    await idp(request).set_enabled(str(user_id), False)
    await idp(request).logout(str(user_id))
    for t in tenant_ids:
        await bump_authz_epoch(redis(request), t)
    return {"id": str(user_id), "status": "deactivated", "sessions_revoked": True}


@router.post("/users/{user_id}/reactivate")
async def reactivate_user(
    user_id: uuid.UUID, body: UserAction, request: Request, ctx: perm("platform:users:manage"), _: StepUp
) -> dict[str, Any]:
    async with db(request).global_session(ctx.actor) as s:
        r = await s.execute(update(users).where(users.c.id == user_id).values(status="active", updated_at=func.now()))
        if r.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFound("User not found")
        await audit(s, ctx.actor, "user.reactivated", "user", user_id, tenant_id=None, reason=body.reason)
    await idp(request).set_enabled(str(user_id), True)
    return {"id": str(user_id), "status": "active"}


@router.post("/users/{user_id}/revoke-sessions")
async def revoke_sessions(
    user_id: uuid.UUID, body: UserAction, request: Request, ctx: perm("platform:users:manage"), _: StepUp
) -> dict[str, Any]:
    await idp(request).logout(str(user_id))
    async with db(request).global_session(ctx.actor) as s:
        await audit(s, ctx.actor, "user.sessions_revoked", "user", user_id, tenant_id=None, reason=body.reason)
    return {"id": str(user_id), "sessions_revoked": True}


# ---------------------------------------------------------------- IAM: roles, platform admins, IdP
@router.get("/iam/roles")
async def iam_roles(request: Request, ctx: perm("platform:iam:read")) -> dict[str, Any]:
    async with db(request).global_session(ctx.actor) as s:
        rows = (
            (
                await s.execute(
                    select(
                        roles.c.key,
                        roles.c.name,
                        roles.c.scope,
                        roles.c.description,
                        roles.c.is_system,
                        roles.c.requires_mfa,
                        func.array_agg(role_permissions.c.permission_key).label("permissions"),
                    )
                    .select_from(roles.outerjoin(role_permissions, role_permissions.c.role_id == roles.c.id))
                    .group_by(roles.c.id)
                    .order_by(roles.c.scope, roles.c.name)
                )
            )
            .mappings()
            .all()
        )
    return {
        "roles": [{**dict(r), "permissions": sorted(p for p in r["permissions"] if p)} for r in rows],
        "catalogue": {"tenant": TENANT_PERMISSIONS, "platform": PLATFORM_PERMISSIONS},
    }


class RoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    name: str = Field(min_length=2, max_length=120)
    description: str = Field("", max_length=500)
    permissions: list[str] = Field(min_length=1, max_length=64)
    requires_mfa: bool = False


@router.post("/iam/roles", status_code=201)
async def create_role(body: RoleIn, request: Request, ctx: perm("platform:iam:manage"), _: StepUp) -> dict[str, Any]:
    """Custom tenant roles composed ONLY from the controlled catalogue (no free-form permissions)."""
    unknown = set(body.permissions) - set(TENANT_PERMISSIONS)
    if unknown:
        raise Unprocessable(f"Permissions not in the tenant catalogue: {sorted(unknown)}")
    if body.key in ROLE_BY_KEY:
        raise Conflict("System role keys are reserved")
    async with db(request).global_session(ctx.actor) as s:
        role_id = (
            await s.execute(
                insert(roles)
                .values(
                    key=body.key,
                    name=body.name,
                    scope="tenant",
                    description=body.description,
                    is_system=False,
                    requires_mfa=body.requires_mfa,
                )
                .on_conflict_do_nothing()
                .returning(roles.c.id)
            )
        ).scalar_one_or_none()
        if role_id is None:
            raise Conflict("Role key already exists")
        for p in sorted(set(body.permissions)):
            await s.execute(insert(role_permissions).values(role_id=role_id, permission_key=p))
        await audit(
            s,
            ctx.actor,
            "iam.role.created",
            "role",
            body.key,
            tenant_id=None,
            permissions=sorted(set(body.permissions)),
        )
    return {"key": body.key}


class PlatformAdminIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: uuid.UUID
    role_key: Literal["platform_owner", "platform_operator"]
    reason: str = Field(min_length=10, max_length=500)


@router.post("/iam/platform-admins", status_code=202)
async def request_platform_admin(
    body: PlatformAdminIn, request: Request, ctx: perm("platform:iam:manage"), _: StepUp
) -> dict[str, Any]:
    """Critical change: requires a second platform admin's approval (dual control)."""
    if body.role_key == "platform_owner" and "platform_owner" not in ctx.principal.realm_roles:
        raise Forbidden("Only a platform owner can propose another platform owner")
    async with db(request).global_session(ctx.actor) as s:
        if (
            await s.execute(select(users.c.id).where(users.c.id == body.user_id, users.c.status == "active"))
        ).first() is None:
            raise NotFound("Active user not found")
        req_id = (
            await s.execute(
                insert(privileged_requests)
                .values(
                    kind="grant_platform_role",
                    payload={"user_id": str(body.user_id), "role_key": body.role_key},
                    reason=body.reason,
                    requested_by=uuid.UUID(ctx.principal.sub),
                    expires_at=datetime.now(UTC) + timedelta(hours=24),
                )
                .returning(privileged_requests.c.id)
            )
        ).scalar_one()
        await audit(
            s,
            ctx.actor,
            "iam.platform_role.requested",
            "privileged_request",
            req_id,
            tenant_id=None,
            target_user=str(body.user_id),
            role=body.role_key,
        )
    return {"request_id": str(req_id), "status": "pending_approval"}


@router.get("/iam/privileged-requests")
async def list_privileged(request: Request, ctx: perm("platform:iam:read")) -> dict[str, Any]:
    async with db(request).global_session(ctx.actor) as s:
        rows = (
            (await s.execute(select(privileged_requests).order_by(privileged_requests.c.created_at.desc()).limit(100)))
            .mappings()
            .all()
        )
    return {"items": [_row(r) for r in rows]}


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool
    note: str = Field("", max_length=500)


@router.post("/iam/privileged-requests/{request_id}/decision")
async def decide_privileged(
    request_id: uuid.UUID, body: Decision, request: Request, ctx: perm("platform:iam:manage"), _: StepUp
) -> dict[str, Any]:
    approver = uuid.UUID(ctx.principal.sub)
    async with db(request).global_session(ctx.actor) as s:
        row = (
            (
                await s.execute(
                    select(privileged_requests).where(privileged_requests.c.id == request_id).with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if row is None or row["status"] != "pending":
            raise NotFound("Request not found or already decided")
        if row["requested_by"] == approver:
            raise Forbidden("A different administrator must approve this request")
        if row["expires_at"] <= datetime.now(UTC):
            await s.execute(
                update(privileged_requests).where(privileged_requests.c.id == request_id).values(status="expired")
            )
            raise Conflict("Request expired")
        payload = row["payload"]
        if (
            body.approve
            and payload["role_key"] == "platform_owner"
            and "platform_owner" not in ctx.principal.realm_roles
        ):
            raise Forbidden("Only a platform owner can approve a platform owner grant")
        await s.execute(
            update(privileged_requests)
            .where(privileged_requests.c.id == request_id)
            .values(status="approved" if body.approve else "rejected", approved_by=approver, updated_at=func.now())
        )
        if body.approve:
            await s.execute(
                insert(platform_role_assignments)
                .values(user_id=uuid.UUID(payload["user_id"]), role_key=payload["role_key"], granted_by=approver)
                .on_conflict_do_nothing()
            )
        await audit(
            s,
            ctx.actor,
            "iam.platform_role." + ("granted" if body.approve else "rejected"),
            "privileged_request",
            request_id,
            tenant_id=None,
            role=payload["role_key"],
            target_user=payload["user_id"],
        )
    if body.approve:
        await idp(request).set_realm_role(payload["user_id"], payload["role_key"], True)
    return {"request_id": str(request_id), "status": "approved" if body.approve else "rejected"}


@router.delete("/iam/platform-admins/{user_id}/{role_key}")
async def remove_platform_admin(
    user_id: uuid.UUID, role_key: str, request: Request, ctx: perm("platform:iam:manage"), _: StepUp
) -> dict[str, Any]:
    if str(user_id) == ctx.principal.sub:
        raise Forbidden("You cannot remove your own platform role")
    async with db(request).global_session(ctx.actor) as s:
        if role_key == "platform_owner":
            owners = (
                await s.execute(
                    select(func.count())
                    .select_from(platform_role_assignments)
                    .where(platform_role_assignments.c.role_key == "platform_owner")
                )
            ).scalar_one()
            if owners <= 1:
                raise Conflict("The platform must keep at least one owner")
        r = await s.execute(
            platform_role_assignments.delete().where(
                platform_role_assignments.c.user_id == user_id, platform_role_assignments.c.role_key == role_key
            )
        )
        if r.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFound("Assignment not found")
        await audit(s, ctx.actor, "iam.platform_role.removed", "user", user_id, tenant_id=None, role=role_key)
    await idp(request).set_realm_role(str(user_id), role_key, False)
    await idp(request).logout(str(user_id))
    return {"removed": True}


@router.get("/iam/idp")
async def idp_status(request: Request, ctx: perm("platform:iam:read")) -> dict[str, Any]:
    s = request.app.state.settings
    status: dict[str, Any] = {
        "issuer": s.oidc_issuer,
        "audience": s.oidc_audience,
        "allowed_algorithms": list(s.oidc_allowed_algs),
    }
    try:
        status |= await idp(request).health()
        async with httpx.AsyncClient(timeout=3.0) as c:
            jwks = (await c.get(s.jwks_url)).json()
        status["signing_keys"] = [
            {"kid": k.get("kid"), "alg": k.get("alg")} for k in jwks.get("keys", []) if k.get("use") == "sig"
        ]
    except Exception as exc:  # noqa: BLE001 - IdP outage must not break the view
        status |= {"reachable": False, "error": type(exc).__name__}
    return status


@router.get("/iam/auth-events")
async def auth_events(
    request: Request, ctx: perm("platform:iam:read"), limit: int = Query(100, ge=1, le=500)
) -> dict[str, Any]:
    return {"items": await idp(request).events(limit)}


# ---------------------------------------------------------------- service accounts
class ServiceAccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9-]+$")
    purpose: str = Field(min_length=10, max_length=500)
    owner_user_id: uuid.UUID
    tenant_id: uuid.UUID | None = None
    scopes: list[str] = Field(min_length=1, max_length=20)
    review_in_days: int = Field(90, ge=7, le=365)


async def _store_client_secret(request: Request, sa_id: uuid.UUID, secret: str) -> str:
    """Client secrets go straight to Vault; the API never returns them (build.md §18.4)."""
    s = request.app.state.settings
    path = f"{s.env.value}/service-accounts/{sa_id}"
    vault = getattr(request.app.state, "vault", None)
    if vault is None:
        raise Unprocessable("Vault is not configured; cannot store service-account credentials")
    await vault.write(path, {"client_secret": secret})
    return path


@router.get("/service-accounts")
async def list_service_accounts(request: Request, ctx: perm("platform:iam:read")) -> dict[str, Any]:
    async with db(request).global_session(ctx.actor) as s:
        rows = (
            (await s.execute(select(service_accounts).order_by(service_accounts.c.created_at.desc()))).mappings().all()
        )
    return {"items": [_row(r) for r in rows]}


@router.post("/service-accounts", status_code=201)
async def create_service_account(
    body: ServiceAccountIn, request: Request, ctx: perm("platform:iam:manage"), _: StepUp
) -> dict[str, Any]:
    unknown = set(body.scopes) - set(TENANT_PERMISSIONS)
    if unknown:
        raise Unprocessable(f"Scopes must come from the tenant permission catalogue: {sorted(unknown)}")
    sa_id = uuid.uuid4()
    client_id = f"sa-{body.name}"
    secret = await idp(request).create_service_client(client_id, body.purpose)
    ref = await _store_client_secret(request, sa_id, secret)
    del secret
    async with db(request).global_session(ctx.actor) as s:
        await s.execute(
            insert(service_accounts).values(
                id=sa_id,
                name=body.name,
                client_id=client_id,
                owner_user_id=body.owner_user_id,
                purpose=body.purpose,
                tenant_id=body.tenant_id,
                scopes=sorted(set(body.scopes)),
                secret_ref=ref,
                review_at=datetime.now(UTC) + timedelta(days=body.review_in_days),
                last_rotated_at=func.now(),
            )
        )
        await audit(
            s,
            ctx.actor,
            "iam.service_account.created",
            "service_account",
            sa_id,
            tenant_id=body.tenant_id,
            client_id=client_id,
            scopes=sorted(set(body.scopes)),
        )
    return {
        "id": str(sa_id),
        "client_id": client_id,
        "secret_ref": ref,
        "note": "The client secret was written to Vault and is never shown in the UI.",
    }


@router.post("/service-accounts/{sa_id}/rotate")
async def rotate_service_account(
    sa_id: uuid.UUID, request: Request, ctx: perm("platform:iam:manage"), _: StepUp
) -> dict[str, Any]:
    async with db(request).global_session(ctx.actor) as s:
        row = (await s.execute(select(service_accounts).where(service_accounts.c.id == sa_id))).mappings().first()
    if row is None:
        raise NotFound("Service account not found")
    secret = await idp(request).rotate_client_secret(row["client_id"])
    ref = await _store_client_secret(request, sa_id, secret)
    del secret
    async with db(request).global_session(ctx.actor) as s:
        await s.execute(
            update(service_accounts)
            .where(service_accounts.c.id == sa_id)
            .values(secret_ref=ref, last_rotated_at=func.now(), updated_at=func.now())
        )
        await audit(s, ctx.actor, "iam.service_account.rotated", "service_account", sa_id, tenant_id=row["tenant_id"])
    return {"id": str(sa_id), "rotated": True}


@router.post("/service-accounts/{sa_id}/status")
async def service_account_status(
    sa_id: uuid.UUID, body: Decision, request: Request, ctx: perm("platform:iam:manage"), _: StepUp
) -> dict[str, Any]:
    async with db(request).global_session(ctx.actor) as s:
        row = (await s.execute(select(service_accounts).where(service_accounts.c.id == sa_id))).mappings().first()
        if row is None:
            raise NotFound("Service account not found")
        status = "active" if body.approve else "disabled"
        await s.execute(
            update(service_accounts).where(service_accounts.c.id == sa_id).values(status=status, updated_at=func.now())
        )
        await audit(s, ctx.actor, f"iam.service_account.{status}", "service_account", sa_id, tenant_id=row["tenant_id"])
    await idp(request).set_client_enabled(row["client_id"], body.approve)
    return {"id": str(sa_id), "status": status}


# ---------------------------------------------------------------- platform settings & flags
class SettingsPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: dict[str, Any]
    expected_version: int


@router.get("/settings")
async def get_platform_settings(request: Request, ctx: perm("platform:settings:read")) -> dict[str, Any]:
    async with db(request).global_session(ctx.actor) as s:
        return await all_platform(s)


@router.put("/settings/{section}")
async def put_platform_settings(
    section: str, body: SettingsPut, request: Request, ctx: perm("platform:settings:manage")
) -> dict[str, Any]:
    model = PLATFORM_SECTIONS.get(section)
    if model is None:
        raise NotFound("Unknown settings section")
    if section in HIGH_RISK_PLATFORM_SECTIONS:
        await require_step_up()(ctx)
    try:
        data = model.model_validate(body.data).model_dump(mode="json")
    except ValidationError as exc:
        raise Unprocessable(
            "Invalid settings", extra={"errors": [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]}
        ) from exc
    async with db(request).global_session(ctx.actor) as s:
        before = await platform_section(s, section)
        version = await put_platform_section(s, section, data, uuid.UUID(ctx.principal.sub), body.expected_version)
        await audit(
            s,
            ctx.actor,
            "platform.settings.updated",
            "platform_settings",
            section,
            tenant_id=None,
            section=section,
            version=version,
            changed_fields=sorted(k for k in data if before.get(k) != data[k]),
        )
    return {**data, "_version": version}


@router.get("/feature-flags")
async def get_flags(request: Request, ctx: perm("platform:settings:read")) -> dict[str, Any]:
    async with db(request).global_session(ctx.actor) as s:
        rows = (await s.execute(select(feature_flags).order_by(feature_flags.c.key))).mappings().all()
    return {"items": [_row(r) for r in rows]}


class FlagPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    tenant_overridable: bool = True
    environments: list[Literal["development", "test", "staging", "production"]] = []
    description: str = Field("", max_length=300)


@router.put("/feature-flags/{key}")
async def put_flag(key: str, body: FlagPut, request: Request, ctx: perm("platform:settings:manage")) -> dict[str, Any]:
    if not re.match(r"^[a-z][a-z0-9_.]{2,63}$", key):
        raise Unprocessable("Invalid flag key")
    async with db(request).global_session(ctx.actor) as s:
        await s.execute(
            insert(feature_flags)
            .values(key=key, **body.model_dump())
            .on_conflict_do_update(
                index_elements=[feature_flags.c.key], set_={**body.model_dump(), "updated_at": func.now()}
            )
        )
        await audit(
            s,
            ctx.actor,
            "platform.feature_flag.updated",
            "feature_flag",
            key,
            tenant_id=None,
            enabled=body.enabled,
            environments=body.environments,
        )
    return {"key": key, **body.model_dump()}


# ---------------------------------------------------------------- support / break-glass
class SupportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: uuid.UUID
    scope: Literal["read_config", "read_operations", "read_crm"]
    reason: str = Field(min_length=10, max_length=1000)
    ticket_ref: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.:#-]+$")
    duration_minutes: int = Field(60, ge=15, le=24 * 60)


@router.get("/support-grants")
async def list_support(
    request: Request, ctx: perm("platform:support:request"), tenant_id: uuid.UUID | None = None
) -> dict[str, Any]:
    database = db(request)
    async with database.global_session(ctx.actor) as s:
        tids = [tenant_id] if tenant_id else (await s.execute(select(tenants.c.id))).scalars().all()
    items: list[dict[str, Any]] = []
    for t in tids:  # per-tenant RLS context; no RLS bypass
        async with database.tenant_session(t, ctx.actor) as s:
            items += [
                _row(r)
                for r in (
                    await s.execute(select(support_grants).where(support_grants.c.status.in_(("pending", "active"))))
                )
                .mappings()
                .all()
            ]
    return {"items": sorted(items, key=lambda r: r["created_at"], reverse=True)}


@router.post("/support-grants", status_code=201)
async def request_support(
    body: SupportRequest, request: Request, ctx: perm("platform:support:request"), _: StepUp
) -> dict[str, Any]:
    database = db(request)
    async with database.global_session(ctx.actor) as s:
        max_hours = int((await platform_section(s, "security"))["support_grant_max_hours"])
        if (await s.execute(select(tenants.c.id).where(tenants.c.id == body.tenant_id))).first() is None:
            raise NotFound("Tenant not found")
    minutes = min(body.duration_minutes, max_hours * 60)
    async with database.tenant_session(body.tenant_id, ctx.actor) as s:
        grant_id = (
            await s.execute(
                insert(support_grants)
                .values(
                    tenant_id=body.tenant_id,
                    requested_by=uuid.UUID(ctx.principal.sub),
                    scope=body.scope,
                    reason=body.reason,
                    ticket_ref=body.ticket_ref,
                    status="pending",
                    expires_at=datetime.now(UTC) + timedelta(minutes=minutes),
                )
                .returning(support_grants.c.id)
            )
        ).scalar_one()
        await audit(
            s,
            ctx.actor,
            "support.access_requested",
            "support_grant",
            grant_id,
            tenant_id=body.tenant_id,
            scope=body.scope,
            ticket=body.ticket_ref,
            minutes=minutes,
            permissions=sorted(SUPPORT_SCOPES[body.scope]),
        )
        await enqueue(
            s,
            outbox,
            make_event(
                "notification.requested",
                producer=PRODUCER,
                actor=ActorRef(type="user", id=ctx.actor.id),
                correlation_id=uuid.uuid4(),
                tenant_id=body.tenant_id,
                payload={
                    "template": "support_access_requested",
                    "resource_type": "support_grant",
                    "resource_id": str(grant_id),
                    "channel": "email",
                },
            ),
        )
    return {"id": str(grant_id), "status": "pending", "expires_in_minutes": minutes}


@router.post("/support-grants/{grant_id}/decision")
async def decide_support(
    grant_id: uuid.UUID,
    tenant_id: uuid.UUID,
    body: Decision,
    request: Request,
    ctx: perm("platform:support:approve"),
    _: StepUp,
) -> dict[str, Any]:
    approver = uuid.UUID(ctx.principal.sub)
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        row = (
            (await s.execute(select(support_grants).where(support_grants.c.id == grant_id).with_for_update()))
            .mappings()
            .first()
        )
        if row is None or row["status"] != "pending":
            raise NotFound("Grant not found or already decided")
        if row["requested_by"] == approver:
            raise Forbidden("Support access must be approved by a different administrator")
        status = "active" if body.approve else "rejected"
        await s.execute(
            update(support_grants)
            .where(support_grants.c.id == grant_id)
            .values(status=status, approved_by=approver, updated_at=func.now())
        )
        await audit(
            s,
            ctx.actor,
            f"support.access_{'granted' if body.approve else 'rejected'}",
            "support_grant",
            grant_id,
            tenant_id=tenant_id,
            requester=str(row["requested_by"]),
            note=body.note,
        )
        await enqueue(
            s,
            outbox,
            make_event(
                "notification.requested",
                producer=PRODUCER,
                actor=ActorRef(type="user", id=ctx.actor.id),
                correlation_id=uuid.uuid4(),
                tenant_id=tenant_id,
                payload={
                    "template": f"support_access_{status}",
                    "resource_type": "support_grant",
                    "resource_id": str(grant_id),
                    "channel": "email",
                },
            ),
        )
    await bump_authz_epoch(redis(request), tenant_id)
    return {"id": str(grant_id), "status": status}


@router.post("/support-grants/{grant_id}/revoke")
async def revoke_support(
    grant_id: uuid.UUID, tenant_id: uuid.UUID, request: Request, ctx: perm("platform:support:request")
) -> dict[str, Any]:
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        r = await s.execute(
            update(support_grants)
            .where(support_grants.c.id == grant_id, support_grants.c.status.in_(("pending", "active")))
            .values(status="revoked", revoked_at=func.now(), updated_at=func.now())
        )
        if r.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFound("Grant not found or not active")
        await audit(s, ctx.actor, "support.access_revoked", "support_grant", grant_id, tenant_id=tenant_id)
    await bump_authz_epoch(redis(request), tenant_id)
    return {"id": str(grant_id), "status": "revoked"}


async def expire_support_grants(database: Any) -> int:
    """Periodic job: automatically revoke expired grants (build.md §20.3)."""
    async with database.global_session() as s:
        tids = (await s.execute(select(tenants.c.id))).scalars().all()
    n = 0
    for t in tids:
        async with database.tenant_session(t, Actor("system", "support-expiry")) as s:
            r = await s.execute(
                update(support_grants)
                .where(support_grants.c.status.in_(("pending", "active")), support_grants.c.expires_at <= func.now())
                .values(status="expired", updated_at=func.now())
                .returning(support_grants.c.id)
            )
            for (gid,) in r.all():
                n += 1
                await audit(
                    s, Actor("system", "support-expiry"), "support.access_expired", "support_grant", gid, tenant_id=t
                )
    return n


# ---------------------------------------------------------------- RLS coverage (read-only)
@router.post("/rls/scans", status_code=201)
async def run_rls_scan(request: Request, ctx: perm("platform:security:read")) -> dict[str, Any]:
    settings = request.app.state.settings
    token = await request.app.state.service_token()

    async def one(svc: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=6.0) as c:
                resp = await c.get(
                    f"http://{svc}:8000/internal/v1/rls/coverage", headers={"Authorization": f"Bearer {token}"}
                )
            if resp.status_code != 200:
                return {"service": svc, "available": False, "error": f"http_{resp.status_code}", "tables": []}
            return dict(resp.json())
        except httpx.HTTPError as exc:
            return {"service": svc, "available": False, "error": type(exc).__name__, "tables": []}

    results = await asyncio.gather(*(one(svc) for svc in settings.rls_scan_targets))
    issues = [
        {"service": r["service"], "table": t["table"], "status": t["status"]}
        for r in results
        for t in r.get("tables", [])
        if t["status"] not in ("ok", "platform_scoped")
    ]
    role_issues = [{"service": r["service"], "issue": i} for r in results for i in r.get("role_issues", [])]
    summary = {
        "services_scanned": sum(1 for r in results if r.get("available")),
        "services_unreachable": sum(1 for r in results if not r.get("available")),
        "tables": sum(len(r.get("tables", [])) for r in results),
        "issues": len(issues),
        "role_issues": len(role_issues),
    }
    async with db(request).global_session(ctx.actor) as s:
        scan_id = (
            await s.execute(
                insert(rls_scans)
                .values(started_by=uuid.UUID(ctx.principal.sub), summary=summary, results=list(results))
                .returning(rls_scans.c.id)
            )
        ).scalar_one()
        await audit(s, ctx.actor, "security.rls_scan.completed", "rls_scan", scan_id, tenant_id=None, **summary)
    return {"id": str(scan_id), "summary": summary, "issues": issues, "role_issues": role_issues, "results": results}


@router.get("/rls/scans/latest")
async def latest_rls_scan(request: Request, ctx: perm("platform:security:read")) -> dict[str, Any]:
    async with db(request).global_session(ctx.actor) as s:
        row = (await s.execute(select(rls_scans).order_by(rls_scans.c.created_at.desc()).limit(1))).mappings().first()
    if row is None:
        return {"scan": None}
    return {"scan": _row(row)}

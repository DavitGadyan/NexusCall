"""Invitation lifecycle (build.md §18.3): pending → sent → accepted | expired | revoked | delivery_failed."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import delete, func, select, update

from nexus_core.auth.jwt import Principal
from nexus_core.authz import AuthContext, get_principal, require_permission
from nexus_core.context import Actor
from nexus_core.errors import Forbidden, NotFound, StepUpRequired, Unprocessable
from tenant_service.access import bump_authz_epoch
from tenant_service.api.me import sync_user
from tenant_service.config_store import platform_section
from tenant_service.deps import audit, db, idp, rate_limit, redis
from tenant_service.domain import (
    INVALID_INVITATION,
    check_grantable,
    create_invitation,
    load_open_invitation,
    load_roles,
    lookup_token,
    upsert_membership,
)
from tenant_service.notify import invitation_link, send_invitation_email
from tenant_service.security import mask_email, normalize_email
from tenant_service.tables import invitation_tokens, invitations, tenants

router = APIRouter(prefix="/api/v1/invitations", tags=["invitations"])
ManageUsers = Annotated[AuthContext, Depends(require_permission("tenant:users:manage"))]
MAX_BULK = 200


class InvitationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    role_keys: list[str] = Field(min_length=1, max_length=10)
    team_id: uuid.UUID | None = None


class BulkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[InvitationIn] = Field(min_length=1, max_length=MAX_BULK)


class TokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=128)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "role_keys": row["role_keys"],
        "status": row["status"],
        "expires_at": row["expires_at"].isoformat(),
        "sent_at": row["sent_at"].isoformat() if row["sent_at"] else None,
        "send_count": row["send_count"],
        "delivery_error": row["delivery_error"],
        "created_at": row["created_at"].isoformat(),
    }


async def issue_and_deliver(
    request: Request,
    *,
    tenant_id: uuid.UUID,
    body: InvitationIn,
    actor: Actor,
    granter: AuthContext | None,
    platform_override: bool,
) -> dict[str, Any]:
    """Shared by tenant admins and the platform tenant-creation flow."""
    database = db(request)
    async with database.global_session(actor) as gs:
        ttl = int((await platform_section(gs, "security"))["invitation_ttl_hours"])
        tenant_name = (await gs.execute(select(tenants.c.name).where(tenants.c.id == tenant_id))).scalar_one()
    async with database.tenant_session(tenant_id, actor) as s:
        role_list = await load_roles(s, body.role_keys)
        if not platform_override:
            if granter is None:
                raise Forbidden("A granting user is required")
            check_grantable(granter.permissions, granter.roles, role_list, platform_override=False)
        inv = await create_invitation(
            s,
            tenant_id=tenant_id,
            email=str(body.email),
            role_list=role_list,
            team_id=body.team_id,
            invited_by=uuid.UUID(actor.id) if actor.type == "user" else None,
            ttl_hours=ttl,
        )
        await audit(
            s,
            actor,
            "invitation.created",
            "invitation",
            inv.id,
            tenant_id=tenant_id,
            roles=sorted(body.role_keys),
            email_masked=mask_email(inv.email),
            superseded=str(inv.superseded) if inv.superseded else None,
        )
    # External side effects after commit: ensure an IdP identity exists, then deliver the link.
    error: str | None = None
    try:
        await idp(request).ensure_user(inv.email)
    except Exception:  # noqa: BLE001 - recorded per row / as delivery failure
        error = "idp_unavailable"
    if error is None:
        error = await send_invitation_email(
            request.app,
            to=inv.email,
            tenant_name=tenant_name,
            inviter=granter.principal.name if granter else "NexusCall platform team",
            role_names=[r.name for r in role_list],
            link=invitation_link(request.app.state.settings.public_base_url, inv.raw_token),
            expires_at=inv.expires_at.isoformat(),
        )
    async with database.tenant_session(tenant_id, actor) as s:
        values: dict[str, Any] = {"send_count": invitations.c.send_count + 1, "updated_at": func.now()}
        values |= (
            {"status": "sent", "sent_at": func.now(), "delivery_error": None}
            if error is None
            else {"status": "delivery_failed", "delivery_error": error}
        )
        await s.execute(update(invitations).where(invitations.c.id == inv.id).values(**values))
        if error:
            await audit(
                s,
                actor,
                "invitation.delivery_failed",
                "invitation",
                inv.id,
                tenant_id=tenant_id,
                outcome="failure",
                error=error,
            )
        row = (await s.execute(select(invitations).where(invitations.c.id == inv.id))).mappings().one()
    return _public(dict(row))


@router.get("")
async def list_invitations(request: Request, ctx: ManageUsers, status: str | None = None) -> dict[str, Any]:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        stmt = select(invitations).order_by(invitations.c.created_at.desc()).limit(500)
        if status:
            stmt = stmt.where(invitations.c.status == status)
        rows = (await s.execute(stmt)).mappings().all()
    return {"items": [_public(dict(r)) for r in rows]}


@router.post("", status_code=201)
async def invite(body: InvitationIn, request: Request, ctx: ManageUsers) -> dict[str, Any]:
    return await issue_and_deliver(
        request, tenant_id=ctx.require_tenant(), body=body, actor=ctx.actor, granter=ctx, platform_override=False
    )


@router.post("/bulk")
async def bulk_invite(body: BulkIn, request: Request, ctx: ManageUsers) -> dict[str, Any]:
    """Per-row validation; one transaction per row; downloadable result report (build.md §18.3)."""
    results = []
    for i, row in enumerate(body.rows, start=1):
        try:
            inv = await issue_and_deliver(
                request, tenant_id=ctx.require_tenant(), body=row, actor=ctx.actor, granter=ctx, platform_override=False
            )
            results.append(
                {
                    "row": i,
                    "email": str(row.email),
                    "result": "invited",
                    "status": inv["status"],
                    "invitation_id": inv["id"],
                }
            )
        except Exception as exc:  # noqa: BLE001 - recorded per row / as delivery failure
            results.append(
                {
                    "row": i,
                    "email": str(row.email),
                    "result": "error",
                    "error": getattr(exc, "detail", "Unexpected error"),
                }
            )
    ok = sum(1 for r in results if r["result"] == "invited")
    return {"summary": {"total": len(results), "invited": ok, "failed": len(results) - ok}, "rows": results}


@router.post("/{invitation_id}/resend")
async def resend(invitation_id: uuid.UUID, request: Request, ctx: ManageUsers) -> dict[str, Any]:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        row = await load_open_invitation(s, invitation_id)
    if row is None:
        raise NotFound(INVALID_INVITATION)
    # A resend issues a fresh token and supersedes the old one.
    return await issue_and_deliver(
        request,
        tenant_id=tenant_id,
        actor=ctx.actor,
        granter=ctx,
        platform_override=False,
        body=InvitationIn(email=row["email"], role_keys=row["role_keys"], team_id=row["team_id"]),
    )


@router.post("/{invitation_id}/revoke")
async def revoke(invitation_id: uuid.UUID, request: Request, ctx: ManageUsers) -> dict[str, Any]:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        result = await s.execute(
            update(invitations)
            .where(invitations.c.id == invitation_id, invitations.c.status.in_(("pending", "sent", "delivery_failed")))
            .values(status="revoked", revoked_at=func.now(), updated_at=func.now())
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFound("Invitation not found or no longer open")
        await s.execute(delete(invitation_tokens).where(invitation_tokens.c.invitation_id == invitation_id))
        await audit(s, ctx.actor, "invitation.revoked", "invitation", invitation_id, tenant_id=tenant_id)
    return {"id": str(invitation_id), "status": "revoked"}


async def _resolve_open(request: Request, raw: str) -> tuple[uuid.UUID, dict[str, Any]]:
    database = db(request)
    async with database.global_session() as gs:
        found = await lookup_token(gs, raw)
    if found is None:
        raise NotFound(INVALID_INVITATION)
    tenant_id, invitation_id = found
    async with database.tenant_session(tenant_id) as s:
        row = await load_open_invitation(s, invitation_id)
    if row is None:
        raise NotFound(INVALID_INVITATION)
    return tenant_id, row


@router.post("/preview")
async def preview(body: TokenIn, request: Request) -> dict[str, Any]:
    """Unauthenticated: shows what the invitation is for. Rate-limited; uniform errors (no enumeration)."""
    await rate_limit(request, "invite-preview", limit=20)
    tenant_id, row = await _resolve_open(request, body.token)
    async with db(request).global_session() as gs:
        name = (await gs.execute(select(tenants.c.name).where(tenants.c.id == tenant_id))).scalar_one()
    return {
        "tenant_name": name,
        "email_masked": mask_email(row["email"]),
        "role_keys": row["role_keys"],
        "expires_at": row["expires_at"].isoformat(),
    }


@router.post("/onboard", status_code=202)
async def onboard(body: TokenIn, request: Request) -> Response:
    """Unauthenticated: for invitees without credentials, asks the IdP to email a set-password /
    verify-email / (admin roles) configure-MFA link that returns to the acceptance page."""
    await rate_limit(request, "invite-onboard", limit=5)
    try:
        tenant_id, row = await _resolve_open(request, body.token)
        user = await idp(request).find_user_by_email(row["email"])
        if user is not None and not user.has_password:
            async with db(request).tenant_session(tenant_id) as s:
                needs_mfa = any(r.requires_mfa for r in await load_roles(s, row["role_keys"]))
            actions = ["VERIFY_EMAIL", "UPDATE_PASSWORD"] + (
                ["CONFIGURE_TOTP"] if needs_mfa and not user.has_otp else []
            )
            settings = request.app.state.settings
            await idp(request).send_actions_email(
                user.id,
                actions,
                f"{settings.public_base_url}/invite/accept?token={body.token}",
                settings.bff_client_id,
                lifespan_seconds=3600,
            )
    except NotFound:
        pass  # identical response for invalid tokens
    return Response(status_code=202)


@router.post("/accept")
async def accept(
    body: TokenIn, request: Request, principal: Annotated[Principal, Depends(get_principal)]
) -> dict[str, Any]:
    await rate_limit(request, "invite-accept", limit=10)
    user_id = await sync_user(request, principal)
    database = db(request)
    async with database.global_session() as gs:
        found = await lookup_token(gs, body.token)
    if found is None:
        raise NotFound(INVALID_INVITATION)
    tenant_id, invitation_id = found
    actor = Actor("user", principal.sub)
    invalid = False
    async with database.tenant_session(tenant_id, actor) as s:
        row = await load_open_invitation(s, invitation_id, lock=True)
        if row is None:
            invalid = True
        else:
            # The authenticated identity must be the intended recipient with a verified email.
            if not principal.email_verified or normalize_email(principal.email or "") != row["email"]:
                await audit(
                    s,
                    actor,
                    "invitation.accept_denied",
                    "invitation",
                    invitation_id,
                    tenant_id=tenant_id,
                    outcome="denied",
                    reason="identity_mismatch",
                )
                raise Forbidden("Sign in with the verified email address this invitation was sent to")
            role_list = await load_roles(s, row["role_keys"])
            if any(r.requires_mfa for r in role_list) and principal.acr not in ("aal2", "aal3"):
                raise StepUpRequired("This role requires multi-factor authentication", extra={"required_acr": "aal2"})
            membership_id = await upsert_membership(
                s,
                tenant_id=tenant_id,
                user_id=user_id,
                role_list=role_list,
                granted_by=row["invited_by"],
                team_id=row["team_id"],
            )
            done = await s.execute(
                update(invitations)
                .where(
                    invitations.c.id == invitation_id, invitations.c.status.in_(("pending", "sent", "delivery_failed"))
                )
                .values(status="accepted", accepted_by=user_id, accepted_at=func.now(), updated_at=func.now())
            )
            if done.rowcount != 1:  # type: ignore[attr-defined]
                raise Unprocessable(INVALID_INVITATION)
            await s.execute(delete(invitation_tokens).where(invitation_tokens.c.invitation_id == invitation_id))
            await audit(
                s,
                actor,
                "invitation.accepted",
                "invitation",
                invitation_id,
                tenant_id=tenant_id,
                membership_id=str(membership_id),
                roles=row["role_keys"],
            )
    if invalid:
        raise NotFound(INVALID_INVITATION)
    await bump_authz_epoch(redis(request), tenant_id)
    return {"tenant_id": str(tenant_id), "membership_id": str(membership_id), "roles": row["role_keys"]}  # type: ignore[index]

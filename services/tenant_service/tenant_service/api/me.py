"""Current-user endpoints: JIT profile sync and tenant list (no cross-tenant data reads)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from nexus_core.auth.jwt import Principal
from nexus_core.authz import AuthDep, get_principal
from nexus_core.errors import Forbidden
from tenant_service.deps import db
from tenant_service.tables import membership_directory, tenants, users

router = APIRouter(prefix="/api/v1/me", tags=["me"])


async def sync_user(request: Request, principal: Principal) -> uuid.UUID:
    if principal.is_service:
        raise Forbidden("Service identities have no user profile")
    user_id = uuid.UUID(principal.sub)
    async with db(request).global_session() as s:
        await s.execute(
            insert(users)
            .values(id=user_id, email=(principal.email or "").lower(), display_name=principal.name)
            .on_conflict_do_update(
                index_elements=[users.c.id],
                set_={
                    "email": (principal.email or "").lower(),
                    "display_name": principal.name,
                    "last_seen_at": func.now(),
                    "updated_at": func.now(),
                },
            )
        )
        status = (await s.execute(select(users.c.status).where(users.c.id == user_id))).scalar_one()
    if status != "active":
        raise Forbidden("Account is deactivated")
    return user_id


@router.get("")
async def me(request: Request, principal: Annotated[Principal, Depends(get_principal)]) -> dict[str, Any]:
    user_id = await sync_user(request, principal)
    async with db(request).global_session() as s:
        rows = (
            await s.execute(
                select(tenants.c.id, tenants.c.name, tenants.c.slug, tenants.c.status)
                .select_from(membership_directory.join(tenants, tenants.c.id == membership_directory.c.tenant_id))
                .where(
                    membership_directory.c.user_id == user_id,
                    membership_directory.c.status == "active",
                    tenants.c.status == "active",
                )
                .order_by(tenants.c.name)
            )
        ).all()
    platform = await request.app.state.authz_resolver.resolve_platform(principal)
    return {
        "id": str(user_id),
        "email": principal.email,
        "name": principal.name,
        "tenants": [{"id": str(r.id), "name": r.name, "slug": r.slug} for r in rows],
        "platform_permissions": sorted(platform),
        "mfa": {"acr": principal.acr},
    }


@router.get("/access")
async def access(ctx: AuthDep) -> dict[str, Any]:
    """Effective permissions in the selected tenant (UI gating only; backend stays authoritative)."""
    return {
        "tenant_id": str(ctx.tenant_id) if ctx.tenant_id else None,
        "roles": sorted(ctx.roles),
        "permissions": sorted(ctx.permissions),
        "platform_permissions": sorted(ctx.platform_permissions),
    }

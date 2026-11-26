"""Platform Admin Console: secrets health (metadata only) and platform-level provider connections
(build.md §18.1A, §19.3). `platform:secrets:*` and `platform:integrations:*`; high-risk actions need step-up."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from integration_service import connections as lifecycle
from integration_service.api.models import (
    ConnectionOut,
    ConnectionPage,
    PlatformConnectionCreate,
    RotateIn,
    SecretRefPage,
)
from integration_service.connections import Scope, public
from integration_service.deps import audit, db
from integration_service.jobs import check_vault_health
from integration_service.tables import platform_connections, secret_registry
from nexus_core.authz import AuthContext, require_permission, require_step_up
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])
SecretsRead = Annotated[AuthContext, Depends(require_permission("platform:secrets:read"))]
SecretsManage = Annotated[AuthContext, Depends(require_permission("platform:secrets:manage"))]
IntRead = Annotated[AuthContext, Depends(require_permission("platform:integrations:read"))]
IntManage = Annotated[AuthContext, Depends(require_permission("platform:integrations:manage"))]
StepUp = Annotated[AuthContext, Depends(require_step_up())]


def _health(row: dict[str, Any]) -> str:
    if row["status"] == "revoked":
        return "revoked" if row["last_vault_status"] != "delete_failed" else "revoked_delete_pending"
    if row["last_vault_status"] not in (None, "ok"):
        return "vault_error"
    if row["status"] in ("error", "disabled"):
        return row["status"]
    due = row["next_rotation_due"]
    if due is not None and due <= datetime.now(UTC):
        return "rotation_overdue"
    return "healthy"


@router.get("/secrets", response_model=SecretRefPage)
async def list_secrets(
    request: Request, ctx: SecretsRead, limit: Limit = 50, cursor: Cursor = None,
    scope: Literal["tenant", "platform"] | None = None, provider: str | None = None, status: str | None = None,
    tenant_id: uuid.UUID | None = None,
) -> dict[str, Any]:  # fmt: skip
    """Secret references with owning service, environment, rotation and retrieval status. Never values."""
    t = secret_registry
    stmt = select(t)
    if scope:
        stmt = stmt.where(t.c.scope == scope)
    if provider:
        stmt = stmt.where(t.c.provider == provider)
    if status:
        stmt = stmt.where(t.c.status == status)
    if tenant_id:
        stmt = stmt.where(t.c.tenant_id == tenant_id)
    # vault_ref is the primary key (text); keyset on (created_at, vault_ref) keeps paging stable.
    stmt = apply_keyset(stmt, t.c.created_at, t.c.vault_ref, cursor, limit)
    async with db(request).global_session(ctx.actor) as s:
        rows = [dict(r) for r in (await s.execute(stmt)).mappings().all()]
    page, next_cursor = build_page(rows, limit, id_attr="vault_ref")
    return {"items": [{**r, "health": _health(r)} for r in page], "next_cursor": next_cursor}


@router.post("/secrets/health-check")
async def secrets_health_check(request: Request, ctx: SecretsManage) -> dict[str, int]:
    """Refreshes Vault metadata status for managed paths (reads metadata only, never secret data)."""
    checked = await check_vault_health(request.app)
    async with db(request).global_session(ctx.actor) as s:
        await audit(s, ctx.actor, "platform.secrets.health_checked", "secret_registry", None, tenant_id=None,
                    checked=checked)  # fmt: skip
    return {"checked": checked}


# ------------------------------------------------------------------------------------------- connections
def _scope_of(row: dict[str, Any]) -> Scope:
    return Scope(None, row["owning_service"])


async def _platform_row(request: Request, ctx: AuthContext, connection_id: uuid.UUID) -> dict[str, Any]:
    async with db(request).global_session(ctx.actor) as s:
        return await lifecycle._load(s, Scope(None), connection_id)


@router.get("/integrations/connections", response_model=ConnectionPage)
async def list_platform_connections(request: Request, ctx: IntRead, limit: Limit = 50,
                                    cursor: Cursor = None) -> dict[str, Any]:  # fmt: skip
    t = platform_connections
    stmt = apply_keyset(select(t), t.c.created_at, t.c.id, cursor, limit)
    async with db(request).global_session(ctx.actor) as s:
        rows = [dict(r) for r in (await s.execute(stmt)).mappings().all()]
    page, next_cursor = build_page(rows, limit)
    return {"items": [public(r) for r in page], "next_cursor": next_cursor}


@router.post("/integrations/connections", response_model=ConnectionOut, status_code=201)
async def create_platform_connection(
    body: PlatformConnectionCreate, request: Request, ctx: IntManage, _: StepUp
) -> dict[str, Any]:
    row = await lifecycle.create_connection(
        request.app, Scope(None, body.owning_service), ctx.actor, provider=body.provider,
        credentials=body.credentials, config=body.config, display_name=body.display_name,
    )  # fmt: skip
    return public(row)


@router.post("/integrations/connections/{connection_id}/test", response_model=ConnectionOut)
async def test_platform_connection(connection_id: uuid.UUID, request: Request, ctx: IntManage) -> dict[str, Any]:
    row = await _platform_row(request, ctx, connection_id)
    return public(await lifecycle.test_connection(request.app, _scope_of(row), ctx.actor, connection_id))


@router.post("/integrations/connections/{connection_id}/rotate", response_model=ConnectionOut)
async def rotate_platform_connection(
    connection_id: uuid.UUID, body: RotateIn, request: Request, ctx: IntManage, _: StepUp
) -> dict[str, Any]:
    row = await _platform_row(request, ctx, connection_id)
    return public(await lifecycle.rotate_connection(request.app, _scope_of(row), ctx.actor, connection_id,
                                                    body.credentials, reason=body.reason))  # fmt: skip


@router.post("/integrations/connections/{connection_id}/disable", response_model=ConnectionOut)
async def disable_platform_connection(connection_id: uuid.UUID, request: Request, ctx: IntManage) -> dict[str, Any]:
    row = await _platform_row(request, ctx, connection_id)
    return public(await lifecycle.set_enabled(request.app, _scope_of(row), ctx.actor, connection_id, False))


@router.post("/integrations/connections/{connection_id}/revoke", response_model=ConnectionOut)
async def revoke_platform_connection(
    connection_id: uuid.UUID, request: Request, ctx: IntManage, _: StepUp
) -> dict[str, Any]:
    row = await _platform_row(request, ctx, connection_id)
    return public(await lifecycle.revoke_connection(request.app, _scope_of(row), ctx.actor, connection_id))

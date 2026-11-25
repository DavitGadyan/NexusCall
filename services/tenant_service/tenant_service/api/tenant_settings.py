"""Tenant settings (Tenant Admin Console). Validated, bounded by platform policy, versioned, audited."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, ValidationError

from nexus_core.authz import AuthContext, require_permission
from nexus_core.errors import NotFound, Unprocessable
from tenant_service.config_store import all_platform, put_tenant_section, tenant_section
from tenant_service.deps import audit, db
from tenant_service.settings_schema import TENANT_SECTIONS, BoundsError, enforce_platform_bounds

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])
Manage = Annotated[AuthContext, Depends(require_permission("tenant:manage"))]
# Reading branding/profile/modules is needed by every member to render the UI.
ReadAny = Annotated[AuthContext, Depends(require_permission("tenant:manage"))]
PUBLIC_SECTIONS = frozenset({"profile", "branding", "modules"})


class SectionPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: dict[str, Any]
    expected_version: int


@router.get("")
async def all_sections(request: Request, ctx: Manage) -> dict[str, Any]:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        return {name: await tenant_section(s, tenant_id, name) for name in TENANT_SECTIONS}


@router.get("/{section}")
async def get_section(section: str, request: Request, ctx: Manage) -> dict[str, Any]:
    if section not in TENANT_SECTIONS:
        raise NotFound("Unknown settings section")
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        return await tenant_section(s, tenant_id, section)


@router.put("/{section}")
async def put_section(section: str, body: SectionPut, request: Request, ctx: Manage) -> dict[str, Any]:
    model = TENANT_SECTIONS.get(section)
    if model is None:
        raise NotFound("Unknown settings section")
    try:
        parsed = model.model_validate(body.data)
    except ValidationError as exc:
        raise Unprocessable(
            "Invalid settings", extra={"errors": [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]}
        ) from exc
    tenant_id = ctx.require_tenant()
    database = db(request)
    async with database.global_session(ctx.actor) as gs:
        platform = await all_platform(gs)
    try:
        enforce_platform_bounds(section, parsed, platform)
    except BoundsError as exc:
        raise Unprocessable(str(exc)) from exc
    data = parsed.model_dump(mode="json")
    async with database.tenant_session(tenant_id, ctx.actor) as s:
        before = await tenant_section(s, tenant_id, section)
        version = await put_tenant_section(
            s, tenant_id, section, data, uuid.UUID(ctx.principal.sub), body.expected_version
        )
        changed = sorted(k for k in data if before.get(k) != data[k])
        await audit(
            s,
            ctx.actor,
            "tenant.settings.updated",
            "tenant_settings",
            section,
            tenant_id=tenant_id,
            section=section,
            version=version,
            changed_fields=changed,
        )
    return {**data, "_version": version}

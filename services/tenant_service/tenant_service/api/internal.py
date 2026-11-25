"""Service-to-service API (allow-listed service identities only)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.routing import APIRouter
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from nexus_core.auth.jwt import Principal
from nexus_core.authz import require_service
from nexus_core.errors import NotFound
from tenant_service.config_store import platform_section, tenant_section
from tenant_service.deps import db
from tenant_service.settings import get_settings
from tenant_service.settings_schema import TENANT_SECTIONS
from tenant_service.tables import tenants

router = APIRouter(prefix="/internal/v1", tags=["internal"], include_in_schema=False)
Service = Annotated[Principal, Depends(require_service(*get_settings().internal_client_allowlist))]


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sub: str
    tenant_id: uuid.UUID


class PlatformIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sub: str
    realm_roles: list[str]


@router.post("/authz/resolve")
async def resolve(body: ResolveIn, request: Request, _: Service) -> dict[str, Any]:
    principal = Principal(sub=body.sub, azp=None, is_service=False)
    access = await request.app.state.authz_resolver.resolve_tenant(principal, body.tenant_id)
    return {
        "is_member": access.is_member,
        "permissions": sorted(access.permissions),
        "roles": sorted(access.roles),
        "tenant_status": access.tenant_status,
    }


@router.post("/authz/platform")
async def resolve_platform(body: PlatformIn, request: Request, _: Service) -> dict[str, Any]:
    principal = Principal(sub=body.sub, azp=None, is_service=False, realm_roles=frozenset(body.realm_roles))
    perms = await request.app.state.authz_resolver.resolve_platform(principal)
    return {"permissions": sorted(perms)}


@router.get("/tenants/{tenant_id}")
async def tenant_info(tenant_id: uuid.UUID, request: Request, _: Service) -> dict[str, Any]:
    database = db(request)
    async with database.global_session() as s:
        row = (
            (
                await s.execute(
                    select(tenants.c.id, tenants.c.name, tenants.c.slug, tenants.c.status).where(
                        tenants.c.id == tenant_id
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound("Tenant not found")
        kill = await platform_section(s, "kill_switches")
    async with database.tenant_session(tenant_id) as s:
        profile = await tenant_section(s, tenant_id, "profile")
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "slug": row["slug"],
        "status": row["status"],
        "timezone": profile.get("timezone", "UTC"),
        "platform_kill_switches": kill,
    }


@router.get("/tenants/{tenant_id}/settings/{section}")
async def tenant_setting(tenant_id: uuid.UUID, section: str, request: Request, _: Service) -> dict[str, Any]:
    if section not in TENANT_SECTIONS:
        raise NotFound("Unknown section")
    database = db(request)
    async with database.global_session() as s:
        kill = await platform_section(s, "kill_switches")
        status = (await s.execute(select(tenants.c.status).where(tenants.c.id == tenant_id))).scalar_one_or_none()
    if status is None:
        raise NotFound("Tenant not found")
    async with database.tenant_session(tenant_id) as s:
        data = await tenant_section(s, tenant_id, section)
    if section == "ai":
        data["effective_ai_enabled"] = bool(kill["ai_enabled"] and status == "active")
    if section == "communication":  # effective switches: platform AND tenant AND tenant active
        data["effective_calls_enabled"] = bool(kill["calls_enabled"] and data["calls_enabled"] and status == "active")
        data["effective_emails_enabled"] = bool(
            kill["emails_enabled"] and data["emails_enabled"] and status == "active"
        )
    return data

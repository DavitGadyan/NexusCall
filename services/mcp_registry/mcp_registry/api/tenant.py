"""Tenant AI tool configuration (build.md §24.8 "Tenant AI settings UI"): tool permissions within the
platform-defined maximum. Tenants can only tighten; grants are validated against the agent's owner service."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from mcp_registry.deps import active_policy, audit, db
from mcp_registry.policy import TOOL_ID_PATTERN, effective
from mcp_registry.resolve import load_grants, load_servers, load_settings, load_tools, resolve_for_agent
from mcp_registry.tables import agent_tool_grants, mcp_servers, tenant_tool_settings
from nexus_core.authz import AuthContext, require_permission
from nexus_core.clients import UpstreamError, UpstreamUnavailable, service_client
from nexus_core.errors import NotFound, ServiceUnavailable, Unprocessable
from nexus_core.pagination import Cursor, Limit, decode_cursor, encode_cursor

router = APIRouter(prefix="/api/v1/ai", tags=["ai-tools"])
Reader = Annotated[AuthContext, Depends(require_permission("ai:agents:read"))]
AgentManager = Annotated[AuthContext, Depends(require_permission("ai:agents:manage"))]
TenantManager = Annotated[AuthContext, Depends(require_permission("tenant:manage"))]


class ToolOut(BaseModel):
    tool_id: str
    version: int
    display_name: str
    description: str
    kind: str
    risk_class: int
    side_effecting: bool
    requires_approval: bool
    enabled: bool
    platform_enabled: bool
    reason: str | None
    rate_per_minute: int
    input_schema: dict[str, Any]


class ToolPage(BaseModel):
    items: list[ToolOut]
    next_cursor: str | None


def _page(items: list[Any], key: Any, cursor: str | None, limit: int) -> tuple[list[Any], str | None]:
    """Stable keyset over a small, code-ordered catalogue (ordered by tool_id)."""
    if cursor:
        _, last = decode_cursor(cursor)
        items = [i for i in items if key(i) > last]
    page, more = items[:limit], len(items) > limit
    from datetime import UTC, datetime

    return page, encode_cursor(datetime(1970, 1, 1, tzinfo=UTC), key(page[-1])) if more and page else None


@router.get("/tools", response_model=ToolPage)
async def list_tools(request: Request, ctx: Reader, limit: Limit = 50, cursor: Cursor = None) -> Any:
    """The catalogue as this tenant sees it. Privileged (class 4) tools are never listed to tenants."""
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        _, policy = await active_policy(s)
        tools = [t for t in await load_tools(s) if t["risk_class"] < 4 and not t["revoked"]]
        servers = await load_servers(s, {t["server_id"] for t in tools if t["server_id"]})
        settings = await load_settings(s, tenant_id)
    items = []
    for t in tools:
        server = servers.get(t["server_id"]) if t["server_id"] else None
        eff = effective(t, server, policy, settings.get(t["tool_id"]), {"requires_approval": False})
        items.append(
            ToolOut(
                tool_id=t["tool_id"],
                version=t["version"],
                display_name=t["display_name"],
                description=t["description"],
                kind=t["kind"],
                risk_class=t["risk_class"],
                side_effecting=t["side_effecting"],
                requires_approval=eff.requires_approval,
                enabled=eff.enabled,
                platform_enabled=bool(t["enabled"]),
                reason=eff.reason if not eff.enabled else None,
                rate_per_minute=eff.rate_per_minute,
                input_schema=t["input_schema"],
            )
        )
    page, nxt = _page(items, lambda i: i.tool_id, cursor, limit)
    return ToolPage(items=page, next_cursor=nxt)


# ---------------------------------------------------------------- agent grants
class GrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_id: str = Field(max_length=80, pattern=TOOL_ID_PATTERN)
    requires_approval: bool = False
    calls_per_minute: int | None = Field(default=None, ge=1, le=6000)


class GrantsPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grants: list[GrantIn] = Field(max_length=50)


class GrantOut(BaseModel):
    tool_id: str
    risk_class: int
    requires_approval: bool
    effective_requires_approval: bool
    calls_per_minute: int | None
    usable: bool
    reason: str | None


class GrantsOut(BaseModel):
    agent_id: uuid.UUID
    items: list[GrantOut]
    next_cursor: str | None = None


async def _agent_exists(request: Request, ctx: AuthContext, agent_id: uuid.UUID) -> None:
    """The agent is owned by ai-agent-service: verify it exists in THIS tenant (no blind grants)."""
    client = service_client(request.app, "ai-agent-service")
    try:
        agent = await client.get_json(f"/internal/v1/agents/{agent_id}", tenant_id=ctx.tenant_id, actor=ctx.actor)
    except UpstreamError as exc:
        if exc.is_not_found:
            raise NotFound("Agent not found") from exc
        raise ServiceUnavailable("Agent service rejected the lookup") from exc
    except UpstreamUnavailable as exc:
        raise ServiceUnavailable("Agent service is unavailable") from exc
    if str(agent.get("agent_id")) != str(agent_id) or agent.get("status") == "archived":
        raise NotFound("Agent not found")


async def _grants_out(request: Request, ctx: AuthContext, agent_id: uuid.UUID) -> GrantsOut:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        _, policy = await active_policy(s)
        grants = await load_grants(s, tenant_id, agent_id)
        resolved = await resolve_for_agent(s, tenant_id, agent_id, policy, sorted(grants)) if grants else []
    items = [
        GrantOut(
            tool_id=t["tool_id"],
            risk_class=t["risk_class"],
            requires_approval=bool(grants[t["tool_id"]]["requires_approval"]),
            effective_requires_approval=eff.requires_approval,
            calls_per_minute=grants[t["tool_id"]]["calls_per_minute"],
            usable=eff.granted,
            reason=eff.reason,
        )
        for t, _server, eff in resolved
    ]
    return GrantsOut(agent_id=agent_id, items=items)


@router.get("/grants", response_model=GrantsOut)
async def get_grants(request: Request, ctx: AgentManager, agent_id: uuid.UUID = Query()) -> Any:
    return await _grants_out(request, ctx, agent_id)


@router.put("/grants", response_model=GrantsOut)
async def put_grants(body: GrantsPut, request: Request, ctx: AgentManager, agent_id: uuid.UUID = Query()) -> Any:
    """Replace the agent's grant set. Class 4 can never be granted; class 3 always requires approval."""
    tenant_id = ctx.require_tenant()
    wanted = {g.tool_id: g for g in body.grants}
    if len(wanted) != len(body.grants):
        raise Unprocessable("Each tool may be granted once")
    await _agent_exists(request, ctx, agent_id)
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        tools = {t["tool_id"]: t for t in await load_tools(s, sorted(wanted))}
        for tool_id in wanted:
            tool = tools.get(tool_id)
            if tool is None or tool["revoked"]:
                raise Unprocessable(f"Unknown tool: {tool_id}")
            if tool["risk_class"] >= 4:
                raise Unprocessable(f"{tool_id} is a privileged (class 4) tool and can never be granted to an agent")
        before = await load_grants(s, tenant_id, agent_id)
        await s.execute(
            delete(agent_tool_grants).where(
                agent_tool_grants.c.tenant_id == tenant_id,
                agent_tool_grants.c.agent_id == agent_id,
                agent_tool_grants.c.tool_id.not_in(list(wanted) or [""]),
            )
        )
        for tool_id, g in wanted.items():
            requires = g.requires_approval or tools[tool_id]["risk_class"] >= 3
            await s.execute(
                insert(agent_tool_grants)
                .values(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    tool_id=tool_id,
                    requires_approval=requires,
                    calls_per_minute=g.calls_per_minute,
                    granted_by=str(ctx.actor),
                )
                .on_conflict_do_update(
                    constraint="uq_agent_tool_grants_tool",
                    set_={
                        "requires_approval": requires,
                        "calls_per_minute": g.calls_per_minute,
                        "granted_by": str(ctx.actor),
                        "updated_at": func.now(),
                    },
                )
            )
        await audit(
            s,
            ctx.actor,
            "ai.tool_grants.updated",
            "agent",
            agent_id,
            tenant_id=tenant_id,
            added=sorted(set(wanted) - set(before)),
            removed=sorted(set(before) - set(wanted)),
            approval_required=sorted(
                t for t, g in wanted.items() if g.requires_approval or tools[t]["risk_class"] >= 3
            ),
        )
    return await _grants_out(request, ctx, agent_id)


# ---------------------------------------------------------------- tenant tool settings (tighten-only)
class SettingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_id: str = Field(max_length=80, pattern=TOOL_ID_PATTERN)
    enabled: bool = True
    requires_approval: bool = False
    rate_per_minute: int | None = Field(default=None, ge=1, le=6000)


class SettingsPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: list[SettingIn] = Field(max_length=200)


class SettingOut(BaseModel):
    tool_id: str
    risk_class: int
    platform_enabled: bool
    platform_requires_approval: bool
    platform_rate_per_minute: int
    enabled: bool
    requires_approval: bool
    rate_per_minute: int | None


class SettingsOut(BaseModel):
    items: list[SettingOut]
    next_cursor: str | None = None


async def _settings_out(request: Request, ctx: AuthContext) -> SettingsOut:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        tools = [t for t in await load_tools(s) if t["risk_class"] < 4 and not t["revoked"]]
        settings = await load_settings(s, tenant_id)
    items = []
    for t in tools:
        st = settings.get(t["tool_id"])
        items.append(
            SettingOut(
                tool_id=t["tool_id"],
                risk_class=t["risk_class"],
                platform_enabled=bool(t["enabled"]),
                platform_requires_approval=bool(t["requires_approval"]),
                platform_rate_per_minute=t["rate_per_minute"],
                enabled=bool(st["enabled"]) if st else True,
                requires_approval=bool(st["requires_approval"]) if st else False,
                rate_per_minute=st["rate_per_minute"] if st else None,
            )
        )
    return SettingsOut(items=items)


@router.get("/tools/settings", response_model=SettingsOut)
async def get_tool_settings(request: Request, ctx: TenantManager) -> Any:
    return await _settings_out(request, ctx)


@router.put("/tools/settings", response_model=SettingsOut)
async def put_tool_settings(body: SettingsPut, request: Request, ctx: TenantManager) -> Any:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        tools = {t["tool_id"]: t for t in await load_tools(s, [x.tool_id for x in body.settings])}
        changed = []
        for item in body.settings:
            tool = tools.get(item.tool_id)
            if tool is None or tool["revoked"] or tool["risk_class"] >= 4:
                raise Unprocessable(f"Unknown tool: {item.tool_id}")
            # Tighten only: a tenant can disable, add approval or lower the rate. Never the reverse.
            if tool["requires_approval"] and not item.requires_approval:
                raise Unprocessable(f"{item.tool_id}: the platform requires approval for this tool")
            if item.rate_per_minute is not None and item.rate_per_minute > tool["rate_per_minute"]:
                raise Unprocessable(f"{item.tool_id}: rate exceeds the platform limit of {tool['rate_per_minute']}")
            values = {
                "enabled": item.enabled,
                "requires_approval": item.requires_approval,
                "rate_per_minute": item.rate_per_minute,
                "updated_by": str(ctx.actor),
            }
            await s.execute(
                insert(tenant_tool_settings)
                .values(tenant_id=tenant_id, tool_id=item.tool_id, **values)
                .on_conflict_do_update(
                    constraint="uq_tenant_tool_settings_tool", set_={**values, "updated_at": func.now()}
                )
            )
            changed.append(item.tool_id)
        await audit(
            s, ctx.actor, "ai.tool_settings.updated", "tenant_tool_settings", None, tenant_id=tenant_id, tools=changed
        )
    return await _settings_out(request, ctx)


# ---------------------------------------------------------------- tenant view of MCP servers
class TenantServerOut(BaseModel):
    id: uuid.UUID
    key: str
    display_name: str
    purpose: str
    trust_level: str
    health: str
    enabled: bool


class TenantServerPage(BaseModel):
    items: list[TenantServerOut]
    next_cursor: str | None = None


@router.get("/mcp-servers", response_model=TenantServerPage)
async def tenant_mcp_servers(request: Request, ctx: Reader) -> Any:
    """Approved servers only, without endpoints, identities or Vault references."""
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        rows = (
            await s.execute(
                select(mcp_servers).where(mcp_servers.c.revoked.is_(False)).order_by(mcp_servers.c.key).limit(200)
            )
        ).mappings()
        items = [TenantServerOut.model_validate(dict(r), from_attributes=False) for r in rows]
    return TenantServerPage(items=items)

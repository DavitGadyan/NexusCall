"""Read-side resolution of tools for one tenant/agent. All tenant rows are read inside a tenant session."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_registry.policy import Effective, ToolPolicy, effective
from mcp_registry.tables import agent_tool_grants, mcp_servers, tenant_tool_settings, tool_definitions


async def load_tools(session: AsyncSession, tool_ids: list[str] | None = None) -> list[dict[str, Any]]:
    stmt = select(tool_definitions).order_by(tool_definitions.c.tool_id)
    if tool_ids is not None:
        stmt = stmt.where(tool_definitions.c.tool_id.in_(tool_ids))
    return [dict(r) for r in (await session.execute(stmt)).mappings().all()]


async def load_servers(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    if not ids:
        return {}
    rows = (await session.execute(select(mcp_servers).where(mcp_servers.c.id.in_(ids)))).mappings().all()
    return {r["id"]: dict(r) for r in rows}


async def load_settings(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    rows = (
        await session.execute(select(tenant_tool_settings).where(tenant_tool_settings.c.tenant_id == tenant_id))
    ).mappings()
    return {r["tool_id"]: dict(r) for r in rows}


async def load_grants(session: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    rows = (
        await session.execute(
            select(agent_tool_grants).where(
                agent_tool_grants.c.tenant_id == tenant_id, agent_tool_grants.c.agent_id == agent_id
            )
        )
    ).mappings()
    return {r["tool_id"]: dict(r) for r in rows}


async def resolve_for_agent(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    policy: ToolPolicy,
    tool_ids: list[str] | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any] | None, Effective]]:
    tools = await load_tools(session, tool_ids)
    servers = await load_servers(session, {t["server_id"] for t in tools if t["server_id"]})
    settings = await load_settings(session, tenant_id)
    grants = await load_grants(session, tenant_id, agent_id)
    out = []
    for tool in tools:
        server = servers.get(tool["server_id"]) if tool["server_id"] else None
        out.append(
            (tool, server, effective(tool, server, policy, settings.get(tool["tool_id"]), grants.get(tool["tool_id"])))
        )
    return out

"""Service-to-service API (BUILD_CONVENTIONS §5). Read-only: no internal endpoint can register servers or
change tools, grants or policy. A model or a service can never write to the registry."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict

from mcp_registry.deps import active_policy, db
from mcp_registry.policy import TOOL_ID_PATTERN
from mcp_registry.resolve import resolve_for_agent
from mcp_registry.settings import get_settings
from nexus_core.authz import InternalContext, require_internal
from nexus_core.errors import NotFound

router = APIRouter(prefix="/internal/v1", tags=["internal"], include_in_schema=False)
_gateway = require_internal(*get_settings().gateway_clients)
_executor = require_internal(*get_settings().executor_clients)


class GrantedTool(BaseModel):
    tool_id: str
    version: int
    description: str
    input_schema: dict[str, Any]
    risk_class: int


class AgentTools(BaseModel):
    tools: list[GrantedTool]
    policy_version: int


@router.get("/agents/{agent_id}/tools", response_model=AgentTools)
async def agent_tools(agent_id: uuid.UUID, request: Request, ictx: InternalContext = Depends(_gateway)) -> Any:
    """Granted, enabled, tenant-allowed, non-privileged tools only. The gateway exposes nothing else."""
    tenant_id = ictx.require_tenant()
    async with db(request).tenant_session(tenant_id, ictx.actor) as s:
        version, policy = await active_policy(s)
        resolved = await resolve_for_agent(s, tenant_id, agent_id, policy)
    tools = [
        GrantedTool(
            tool_id=t["tool_id"],
            version=t["version"],
            description=t["description"],
            input_schema=t["input_schema"],
            risk_class=t["risk_class"],
        )
        for t, _server, eff in resolved
        if eff.granted and t["risk_class"] < 4
    ]
    return AgentTools(tools=tools, policy_version=version)


class GrantCheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: uuid.UUID


class Limits(BaseModel):
    rate_per_minute: int
    agent_calls_per_minute: int
    timeout_ms: int
    max_output_bytes: int


class GrantCheckOut(BaseModel):
    tool_id: str
    granted: bool
    enabled: bool
    reason: str | None
    risk_class: int
    requires_approval: bool
    side_effecting: bool
    version: int
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    target: dict[str, Any]
    limits: Limits
    policy_version: int


@router.post("/tools/{tool_id}/grant-check", response_model=GrantCheckOut)
async def grant_check(
    body: GrantCheckIn,
    request: Request,
    tool_id: str = Path(max_length=80, pattern=TOOL_ID_PATTERN),
    ictx: InternalContext = Depends(_executor),
) -> Any:
    tenant_id = ictx.require_tenant()
    async with db(request).tenant_session(tenant_id, ictx.actor) as s:
        version, policy = await active_policy(s)
        resolved = await resolve_for_agent(s, tenant_id, body.agent_id, policy, [tool_id])
    if not resolved:
        raise NotFound("Unknown tool")
    tool, server, eff = resolved[0]
    target: dict[str, Any] = dict(tool["target"])
    target["kind"] = tool["kind"]
    if tool["kind"] == "mcp" and server is not None:
        # References and limits only. The credential stays in Vault and is read by tool-execution
        # under its own policy, after authorization.
        target.update(
            {
                "server_id": str(server["id"]),
                "server_key": server["key"],
                "endpoint": server["endpoint"],
                "transport": server["transport"],
                "allowed_hosts": list(server["allowed_hosts"]),
                "trust_level": server["trust_level"],
                "service_identity": server["service_identity"],
                "vault_ref": server["vault_ref"],
                "pinned_server_name": server["pinned_server_name"],
                "pinned_version": server["pinned_version"],
                "timeout_ms": server["timeout_ms"],
                "max_concurrency": server["max_concurrency"],
                "server_rate_per_minute": server["rate_per_minute"],
                "max_request_bytes": server["max_request_bytes"],
                "max_response_bytes": server["max_response_bytes"],
            }
        )
    return GrantCheckOut(
        tool_id=tool["tool_id"],
        granted=eff.granted,
        enabled=eff.enabled,
        reason=eff.reason,
        risk_class=tool["risk_class"],
        requires_approval=eff.requires_approval,
        side_effecting=tool["side_effecting"],
        version=tool["version"],
        input_schema=tool["input_schema"],
        output_schema=tool["output_schema"],
        target=target,
        limits=Limits(
            rate_per_minute=eff.rate_per_minute,
            agent_calls_per_minute=policy.agent_calls_per_minute,
            timeout_ms=tool["timeout_ms"],
            max_output_bytes=tool["max_output_bytes"],
        ),
        policy_version=version,
    )

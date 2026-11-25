"""Platform AI control plane (build.md §24.3, §24.8, §24.10). Reads need `platform:ai:read`; every change
needs `platform:ai:manage` plus step-up MFA and is audited. Credentials are Vault references only."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_registry.catalogue import MCP_DEFAULT_OUTPUT_SCHEMA, UnsafeSchema, check_tool_schema
from mcp_registry.deps import active_policy, audit, db, iso, settings
from mcp_registry.policy import TOOL_ID_PATTERN, ToolPolicy
from mcp_registry.settings import Settings
from mcp_registry.tables import ai_policy_versions, mcp_server_versions, mcp_servers, policy_proposals, tool_definitions
from nexus_core.authz import AuthContext, require_permission, require_step_up
from nexus_core.config import Environment
from nexus_core.errors import Conflict, Forbidden, NotFound, Unprocessable
from nexus_core.ssrf import UnsafeDestination, validate_outbound_url
from nexus_core.vault import platform_secret_path

router = APIRouter(prefix="/api/v1/platform/ai", tags=["platform-ai"])
Read = Annotated[AuthContext, Depends(require_permission("platform:ai:read"))]
Manage = Annotated[AuthContext, Depends(require_permission("platform:ai:manage"))]
StepUp = Annotated[AuthContext, Depends(require_step_up())]

HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
IDENTITY_RE = r"^[a-z][a-z0-9-]{2,63}$"
RESERVED_IDENTITY = re.compile(r"(^svc-|admin|root|superuser|owner)")
SERVER_SNAPSHOT_FIELDS = (
    "key", "display_name", "owner", "purpose", "endpoint", "transport", "environment", "trust_level",
    "service_identity", "vault_ref", "allowed_hosts", "timeout_ms", "max_concurrency", "rate_per_minute",
    "max_request_bytes", "max_response_bytes", "pinned_server_name", "pinned_version", "pinned_sha256",
    "reviewed_by", "reviewed_at", "health", "enabled", "disabled_reason", "revoked", "revoked_reason",
)  # fmt: skip


def _uid(ctx: AuthContext) -> uuid.UUID:
    try:
        return uuid.UUID(ctx.principal.sub)
    except ValueError as exc:
        raise Forbidden("A human platform administrator is required") from exc


# ---------------------------------------------------------------- MCP servers
class PinIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9.+_-]+$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="sha256 of the reviewed release or image digest")


class ServerBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=2, max_length=200)
    description: str = Field("", max_length=2000)
    owner: str = Field(min_length=2, max_length=200)
    purpose: str = Field(min_length=5, max_length=2000)
    endpoint: str = Field(min_length=8, max_length=2048)
    environment: str = Field(pattern=r"^[a-z0-9-]{2,32}$")
    trust_level: Literal["internal", "verified", "third_party"]
    service_identity: str = Field(pattern=IDENTITY_RE)
    vault_ref: str | None = Field(default=None, max_length=255)
    allowed_hosts: list[str] = Field(min_length=1, max_length=10)
    timeout_ms: int = Field(5000, ge=100, le=30000)
    max_concurrency: int = Field(4, ge=1, le=64)
    rate_per_minute: int = Field(60, ge=1, le=6000)
    max_request_bytes: int = Field(16384, ge=256, le=262144)
    max_response_bytes: int = Field(65536, ge=256, le=1048576)


class ServerCreate(ServerBase):
    key: str = Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")
    transport: Literal["streamable_http"] = "streamable_http"
    pin: PinIn | None = None


class ServerPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int
    display_name: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    owner: str | None = Field(default=None, min_length=2, max_length=200)
    purpose: str | None = Field(default=None, min_length=5, max_length=2000)
    endpoint: str | None = Field(default=None, min_length=8, max_length=2048)
    trust_level: Literal["internal", "verified", "third_party"] | None = None
    service_identity: str | None = Field(default=None, pattern=IDENTITY_RE)
    vault_ref: str | None = Field(default=None, max_length=255)
    clear_vault_ref: bool = False
    allowed_hosts: list[str] | None = Field(default=None, min_length=1, max_length=10)
    timeout_ms: int | None = Field(default=None, ge=100, le=30000)
    max_concurrency: int | None = Field(default=None, ge=1, le=64)
    rate_per_minute: int | None = Field(default=None, ge=1, le=6000)
    max_request_bytes: int | None = Field(default=None, ge=256, le=262144)
    max_response_bytes: int | None = Field(default=None, ge=256, le=1048576)
    pin: PinIn | None = None


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=5, max_length=500)


class ServerOut(BaseModel):
    id: uuid.UUID
    key: str
    display_name: str
    description: str
    owner: str
    purpose: str
    endpoint: str
    transport: str
    environment: str
    trust_level: str
    service_identity: str
    vault_ref: str | None
    allowed_hosts: list[str]
    timeout_ms: int
    max_concurrency: int
    rate_per_minute: int
    max_request_bytes: int
    max_response_bytes: int
    pinned_server_name: str | None
    pinned_version: str | None
    pinned_sha256: str | None
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    health: str
    last_verified_at: datetime | None
    enabled: bool
    disabled_reason: str | None
    revoked: bool
    revoked_reason: str | None
    row_version: int
    created_at: datetime
    updated_at: datetime


class ServerVersionOut(BaseModel):
    revision: int
    change: str
    changed_by: str
    created_at: datetime
    snapshot: dict[str, Any]


class ServerDetail(ServerOut):
    versions: list[ServerVersionOut]


class ServerPage(BaseModel):
    items: list[ServerOut]
    next_cursor: str | None = None


def _hosts(hosts: list[str]) -> list[str]:
    out = sorted({h.strip().rstrip(".").lower() for h in hosts})
    for h in out:
        if not HOST_RE.match(h):
            raise Unprocessable(f"Invalid allowed host: {h[:80]}")
    return out


async def validate_endpoint(cfg: Settings, endpoint: str, allowed_hosts: list[str]) -> None:
    """Registration-time egress check. tool-execution repeats it on every call (DNS can change)."""
    from urllib.parse import urlsplit

    parts = urlsplit(endpoint)
    if parts.fragment or parts.query:
        raise Unprocessable("MCP endpoint must not contain a query string or fragment")
    host = (parts.hostname or "").rstrip(".").lower()
    private = host in cfg.mcp_private_hosts
    schemes = {"https"}
    if private and cfg.mcp_allow_http_private and cfg.env in (Environment.DEVELOPMENT, Environment.TEST):
        schemes.add("http")
    try:
        await validate_outbound_url(
            endpoint,
            allowed_schemes=frozenset(schemes),
            allowed_hosts=frozenset(allowed_hosts),
            allow_private=private,
        )
    except UnsafeDestination as exc:
        raise Unprocessable(f"MCP endpoint rejected: {exc}") from exc


def _check_vault_ref(cfg: Settings, key: str, vault_ref: str | None) -> None:
    """Each server has its OWN credential path; it can never point at another server's or a shared secret."""
    if vault_ref is None:
        return
    expected = platform_secret_path(cfg.env.value, f"mcp-servers/{key}")
    if vault_ref != expected:
        raise Unprocessable(f"vault_ref must be this server's own credential path: {expected}")


def _check_identity(identity: str) -> None:
    if RESERVED_IDENTITY.search(identity):
        raise Unprocessable("service_identity must be a dedicated least-privilege identity for this server")


def _snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {k: iso(row[k]) for k in SERVER_SNAPSHOT_FIELDS}


async def _history(s: AsyncSession, server_id: uuid.UUID, change: str, actor: str) -> None:
    row = dict((await s.execute(select(mcp_servers).where(mcp_servers.c.id == server_id))).mappings().one())
    revision = (
        await s.execute(
            select(func.coalesce(func.max(mcp_server_versions.c.revision), 0) + 1).where(
                mcp_server_versions.c.server_id == server_id
            )
        )
    ).scalar_one()
    await s.execute(
        mcp_server_versions.insert().values(
            server_id=server_id, revision=revision, change=change, snapshot=_snapshot(row), changed_by=actor
        )
    )


async def _server(s: AsyncSession, server_id: uuid.UUID, *, lock: bool = False) -> dict[str, Any]:
    stmt = select(mcp_servers).where(mcp_servers.c.id == server_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await s.execute(stmt)).mappings().first()
    if row is None:
        raise NotFound("MCP server not found")
    return dict(row)


@router.get("/mcp-servers", response_model=ServerPage)
async def list_servers(request: Request, ctx: Read) -> Any:
    async with db(request).global_session(ctx.actor) as s:
        rows = (await s.execute(select(mcp_servers).order_by(mcp_servers.c.key).limit(200))).mappings().all()
    return ServerPage(items=[ServerOut.model_validate(dict(r)) for r in rows])


@router.get("/mcp-servers/{server_id}", response_model=ServerDetail)
async def get_server(server_id: uuid.UUID, request: Request, ctx: Read) -> Any:
    async with db(request).global_session(ctx.actor) as s:
        row = await _server(s, server_id)
        versions = (
            await s.execute(
                select(mcp_server_versions)
                .where(mcp_server_versions.c.server_id == server_id)
                .order_by(mcp_server_versions.c.revision.desc())
                .limit(100)
            )
        ).mappings()
        history = [ServerVersionOut.model_validate(dict(v)) for v in versions]
    return ServerDetail(**ServerOut.model_validate(row).model_dump(), versions=history)


@router.post("/mcp-servers", response_model=ServerOut, status_code=201)
async def create_server(body: ServerCreate, request: Request, ctx: Manage, _: StepUp) -> Any:
    cfg = settings(request)
    hosts = _hosts(body.allowed_hosts)
    await validate_endpoint(cfg, body.endpoint, hosts)
    _check_vault_ref(cfg, body.key, body.vault_ref)
    _check_identity(body.service_identity)
    values = body.model_dump(exclude={"pin"})
    values["allowed_hosts"] = hosts
    if body.pin:
        values.update(
            pinned_server_name=body.pin.server_name,
            pinned_version=body.pin.version,
            pinned_sha256=body.pin.sha256,
            reviewed_by=_uid(ctx),
            reviewed_at=datetime.now(UTC),
        )
    async with db(request).global_session(ctx.actor) as s:
        clash = (
            await s.execute(
                select(mcp_servers.c.id).where(
                    (mcp_servers.c.key == body.key)
                    | (mcp_servers.c.service_identity == body.service_identity)
                    | ((mcp_servers.c.vault_ref == body.vault_ref) if body.vault_ref else False)
                )
            )
        ).first()
        if clash is not None:
            raise Conflict("Key, service identity or credential reference is already used by another server")
        server_id = (
            await s.execute(
                mcp_servers.insert().values(**values, created_by=_uid(ctx), enabled=False).returning(mcp_servers.c.id)
            )
        ).scalar_one()
        await _history(s, server_id, "created", str(ctx.actor))
        await audit(
            s, ctx.actor, "ai.mcp_server.created", "mcp_server", server_id, tenant_id=None,
            key=body.key, trust_level=body.trust_level, hosts=hosts, pinned=bool(body.pin),
        )  # fmt: skip
        row = await _server(s, server_id)
    return ServerOut.model_validate(row)


IDENTITY_FIELDS = ("endpoint", "allowed_hosts", "vault_ref", "service_identity", "trust_level")


@router.patch("/mcp-servers/{server_id}", response_model=ServerOut)
async def patch_server(server_id: uuid.UUID, body: ServerPatch, request: Request, ctx: Manage, _: StepUp) -> Any:
    cfg = settings(request)
    async with db(request).global_session(ctx.actor) as s:
        row = await _server(s, server_id, lock=True)
        if row["revoked"]:
            raise Conflict("A revoked server cannot be changed; register a new one")
        if row["row_version"] != body.expected_version:
            raise Conflict("The server was changed by someone else; reload and retry")
        changes: dict[str, Any] = body.model_dump(
            exclude_none=True, exclude={"expected_version", "pin", "clear_vault_ref"}
        )
        if body.clear_vault_ref:
            changes["vault_ref"] = None
        if "allowed_hosts" in changes:
            changes["allowed_hosts"] = _hosts(changes["allowed_hosts"])
        endpoint = changes.get("endpoint", row["endpoint"])
        hosts = changes.get("allowed_hosts", list(row["allowed_hosts"]))
        if "endpoint" in changes or "allowed_hosts" in changes:
            await validate_endpoint(cfg, endpoint, hosts)
        if "vault_ref" in changes:
            _check_vault_ref(cfg, row["key"], changes["vault_ref"])
        if "service_identity" in changes:
            _check_identity(changes["service_identity"])
        identity_changed = any(f in changes and changes[f] != row[f] for f in IDENTITY_FIELDS)
        change = "updated"
        if identity_changed:
            # A different destination or identity is a different server: the review no longer applies.
            changes.update(
                pinned_server_name=None, pinned_version=None, pinned_sha256=None, reviewed_by=None,
                reviewed_at=None, enabled=False, disabled_reason="re-review required after identity change",
            )  # fmt: skip
        if body.pin:
            changes.update(
                pinned_server_name=body.pin.server_name,
                pinned_version=body.pin.version,
                pinned_sha256=body.pin.sha256,
                reviewed_by=_uid(ctx),
                reviewed_at=datetime.now(UTC),
                health="unknown",
            )
            if not identity_changed:
                change = "pinned"
            else:
                changes["enabled"] = False
        if not changes:
            raise Unprocessable("Nothing to change")
        await s.execute(
            update(mcp_servers)
            .where(mcp_servers.c.id == server_id)
            .values(**changes, row_version=mcp_servers.c.row_version + 1, updated_at=func.now())
        )
        await _history(s, server_id, change, str(ctx.actor))
        await audit(
            s, ctx.actor, f"ai.mcp_server.{change}", "mcp_server", server_id, tenant_id=None,
            fields=sorted(k for k in changes if k not in ("reviewed_by", "reviewed_at")), review_reset=identity_changed,
        )  # fmt: skip
        return ServerOut.model_validate(await _server(s, server_id))


async def _set_server_state(
    request: Request, ctx: AuthContext, server_id: uuid.UUID, action: str, reason: str | None
) -> dict[str, Any]:
    async with db(request).global_session(ctx.actor) as s:
        row = await _server(s, server_id, lock=True)
        if row["revoked"]:
            raise Conflict("The server is revoked")
        if action == "enable":
            if not (row["pinned_version"] and row["pinned_sha256"] and row["reviewed_by"]):
                raise Conflict("Pin a reviewed version (version, sha256) before enabling the server")
            if row["health"] == "identity_mismatch":
                raise Conflict("The server reported an unexpected identity; review and re-pin it first")
            values: dict[str, Any] = {"enabled": True, "disabled_reason": None}
        elif action == "disable":
            values = {"enabled": False, "disabled_reason": reason}
        else:
            values = {"enabled": False, "revoked": True, "revoked_reason": reason, "disabled_reason": "revoked"}
        await s.execute(
            update(mcp_servers)
            .where(mcp_servers.c.id == server_id)
            .values(**values, row_version=mcp_servers.c.row_version + 1, updated_at=func.now())
        )
        change = {"enable": "enabled", "disable": "disabled", "revoke": "revoked"}[action]
        await _history(s, server_id, change, str(ctx.actor))
        await audit(s, ctx.actor, f"ai.mcp_server.{change}", "mcp_server", server_id, tenant_id=None, reason=reason)
        return await _server(s, server_id)


@router.post("/mcp-servers/{server_id}/enable", response_model=ServerOut)
async def enable_server(server_id: uuid.UUID, request: Request, ctx: Manage, _: StepUp) -> Any:
    return ServerOut.model_validate(await _set_server_state(request, ctx, server_id, "enable", None))


@router.post("/mcp-servers/{server_id}/disable", response_model=ServerOut)
async def disable_server(server_id: uuid.UUID, body: ReasonIn, request: Request, ctx: Manage, _: StepUp) -> Any:
    return ServerOut.model_validate(await _set_server_state(request, ctx, server_id, "disable", body.reason))


@router.post("/mcp-servers/{server_id}/revoke", response_model=ServerOut)
async def revoke_server(server_id: uuid.UUID, body: ReasonIn, request: Request, ctx: Manage, _: StepUp) -> Any:
    """Emergency revocation: permanent; every tool of the server stops immediately."""
    return ServerOut.model_validate(await _set_server_state(request, ctx, server_id, "revoke", body.reason))


# ---------------------------------------------------------------- tools
class ToolDefOut(BaseModel):
    tool_id: str
    version: int
    display_name: str
    description: str
    kind: str
    server_id: uuid.UUID | None
    target: dict[str, Any]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    risk_class: int
    side_effecting: bool
    requires_approval: bool
    data_classification: str
    rate_per_minute: int
    timeout_ms: int
    max_output_bytes: int
    enabled: bool
    disabled_reason: str | None
    revoked: bool
    revoked_reason: str | None
    updated_at: datetime


class ToolDefPage(BaseModel):
    items: list[ToolDefOut]
    next_cursor: str | None = None


class McpToolCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_id: str = Field(max_length=80, pattern=TOOL_ID_PATTERN)
    server_id: uuid.UUID
    tool_name: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$", description="the tool's name on the MCP server")
    display_name: str = Field(min_length=2, max_length=200)
    description: str = Field(min_length=5, max_length=1000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    risk_class: int = Field(ge=0, le=4)
    side_effecting: bool
    requires_approval: bool = False
    data_classification: Literal["public", "internal", "confidential", "restricted"] = "confidential"
    rate_per_minute: int = Field(30, ge=1, le=6000)
    timeout_ms: int = Field(5000, ge=100, le=30000)
    max_output_bytes: int = Field(32768, ge=256, le=1048576)


@router.get("/tools", response_model=ToolDefPage)
async def list_tool_defs(request: Request, ctx: Read) -> Any:
    async with db(request).global_session(ctx.actor) as s:
        rows = (await s.execute(select(tool_definitions).order_by(tool_definitions.c.tool_id))).mappings().all()
    return ToolDefPage(items=[ToolDefOut.model_validate(dict(r)) for r in rows])


@router.post("/tools", response_model=ToolDefOut, status_code=201)
async def register_mcp_tool(body: McpToolCreate, request: Request, ctx: Manage, _: StepUp) -> Any:
    """Register a tool of an approved MCP server. Internal tools ship only through migrations."""
    output_schema = body.output_schema or MCP_DEFAULT_OUTPUT_SCHEMA
    try:
        check_tool_schema(body.input_schema, input_side=True)
        check_tool_schema(output_schema, input_side=False)
    except UnsafeSchema as exc:
        raise Unprocessable(f"Unsafe tool schema: {exc}") from exc
    async with db(request).global_session(ctx.actor) as s:
        server = await _server(s, body.server_id)
        if server["revoked"]:
            raise Conflict("The server is revoked")
        prefix = f"mcp.{server['key'].replace('-', '_')}."
        if not body.tool_id.startswith(prefix):
            # MCP tools live in their server's namespace and can never shadow an internal tool id.
            raise Unprocessable(f"MCP tool ids must start with '{prefix}'")
        if (await s.execute(select(tool_definitions.c.id).where(tool_definitions.c.tool_id == body.tool_id))).first():
            raise Conflict("Tool id already registered")
        await s.execute(
            tool_definitions.insert().values(
                tool_id=body.tool_id,
                display_name=body.display_name,
                description=body.description,
                kind="mcp",
                server_id=body.server_id,
                target={"tool_name": body.tool_name},
                input_schema=body.input_schema,
                output_schema=output_schema,
                risk_class=body.risk_class,
                side_effecting=body.side_effecting or body.risk_class >= 2,
                requires_approval=body.requires_approval or body.risk_class >= 3,
                data_classification=body.data_classification,
                rate_per_minute=min(body.rate_per_minute, server["rate_per_minute"]),
                timeout_ms=min(body.timeout_ms, server["timeout_ms"]),
                max_output_bytes=min(body.max_output_bytes, server["max_response_bytes"]),
                enabled=False,
            )
        )
        await audit(
            s, ctx.actor, "ai.tool.registered", "tool_definition", body.tool_id, tenant_id=None,
            server_id=str(body.server_id), risk_class=body.risk_class,
        )  # fmt: skip
        row = (await s.execute(select(tool_definitions).where(tool_definitions.c.tool_id == body.tool_id))).mappings()
        return ToolDefOut.model_validate(dict(row.one()))


ToolIdPath = Annotated[str, Path(max_length=80, pattern=TOOL_ID_PATTERN)]


async def _set_tool_state(request: Request, ctx: AuthContext, tool_id: str, action: str, reason: str | None) -> Any:
    async with db(request).global_session(ctx.actor) as s:
        row = (
            (await s.execute(select(tool_definitions).where(tool_definitions.c.tool_id == tool_id).with_for_update()))
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound("Tool not found")
        if row["revoked"]:
            raise Conflict("The tool is revoked")
        if action == "enable":
            values: dict[str, Any] = {"enabled": True, "disabled_reason": None}
        elif action == "disable":
            values = {"enabled": False, "disabled_reason": reason}
        else:
            values = {"enabled": False, "revoked": True, "revoked_reason": reason, "disabled_reason": "revoked"}
        await s.execute(
            update(tool_definitions)
            .where(tool_definitions.c.tool_id == tool_id)
            .values(**values, row_version=tool_definitions.c.row_version + 1, updated_at=func.now())
        )
        await audit(
            s, ctx.actor, f"ai.tool.{action}d" if action != "revoke" else "ai.tool.revoked", "tool_definition",
            tool_id, tenant_id=None, reason=reason, risk_class=row["risk_class"],
        )  # fmt: skip
        out = (await s.execute(select(tool_definitions).where(tool_definitions.c.tool_id == tool_id))).mappings()
        return ToolDefOut.model_validate(dict(out.one()))


@router.post("/tools/{tool_id}/enable", response_model=ToolDefOut)
async def enable_tool(tool_id: ToolIdPath, request: Request, ctx: Manage, _: StepUp) -> Any:
    return await _set_tool_state(request, ctx, tool_id, "enable", None)


@router.post("/tools/{tool_id}/disable", response_model=ToolDefOut)
async def disable_tool(tool_id: ToolIdPath, body: ReasonIn, request: Request, ctx: Manage, _: StepUp) -> Any:
    return await _set_tool_state(request, ctx, tool_id, "disable", body.reason)


@router.post("/tools/{tool_id}/revoke", response_model=ToolDefOut)
async def revoke_tool(tool_id: ToolIdPath, body: ReasonIn, request: Request, ctx: Manage, _: StepUp) -> Any:
    """Emergency revocation of one tool for every tenant and agent. Permanent."""
    return await _set_tool_state(request, ctx, tool_id, "revoke", body.reason)


# ---------------------------------------------------------------- policies: propose → approve → deploy → rollback
class PolicyVersionOut(BaseModel):
    version: int
    status: str
    policy: dict[str, Any]
    proposal_id: uuid.UUID | None
    rollback_of: int | None
    deployed_by: str
    deployed_at: datetime


class PoliciesOut(BaseModel):
    active_version: int
    active: dict[str, Any]
    items: list[PolicyVersionOut]
    next_cursor: str | None = None


class ProposalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: dict[str, Any]
    reason: str = Field(min_length=10, max_length=2000)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field("", max_length=500)


class ProposalOut(BaseModel):
    id: uuid.UUID
    base_version: int
    policy: dict[str, Any]
    reason: str
    status: str
    proposed_by: uuid.UUID
    approved_by: uuid.UUID | None
    rejected_by: uuid.UUID | None
    deployed_by: uuid.UUID | None
    decision_note: str | None
    decided_at: datetime | None
    deployed_at: datetime | None
    deployed_version: int | None
    expires_at: datetime
    created_at: datetime


class ProposalPage(BaseModel):
    items: list[ProposalOut]
    next_cursor: str | None = None


@router.get("/policies", response_model=PoliciesOut)
async def list_policies(request: Request, ctx: Read) -> Any:
    async with db(request).global_session(ctx.actor) as s:
        version, policy = await active_policy(s)
        rows = (
            await s.execute(select(ai_policy_versions).order_by(ai_policy_versions.c.version.desc()).limit(100))
        ).mappings()
        items = [PolicyVersionOut.model_validate(dict(r)) for r in rows]
    return PoliciesOut(active_version=version, active=policy.model_dump(), items=items)


@router.get("/policies/proposals", response_model=ProposalPage)
async def list_proposals(request: Request, ctx: Read) -> Any:
    async with db(request).global_session(ctx.actor) as s:
        rows = (
            await s.execute(select(policy_proposals).order_by(policy_proposals.c.created_at.desc()).limit(100))
        ).mappings()
        return ProposalPage(items=[ProposalOut.model_validate(dict(r)) for r in rows])


@router.post("/policies/proposals", response_model=ProposalOut, status_code=201)
async def propose(body: ProposalIn, request: Request, ctx: Manage, _: StepUp) -> Any:
    try:
        policy = ToolPolicy.model_validate(body.policy)
    except ValidationError as exc:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        raise Unprocessable("Invalid policy", extra={"errors": errors}) from exc
    ttl = timedelta(hours=settings(request).policy_proposal_ttl_hours)
    async with db(request).global_session(ctx.actor) as s:
        base, _active = await active_policy(s)
        pid = (
            await s.execute(
                policy_proposals.insert()
                .values(
                    base_version=base,
                    policy=policy.model_dump(),
                    reason=body.reason,
                    proposed_by=_uid(ctx),
                    expires_at=datetime.now(UTC) + ttl,
                )
                .returning(policy_proposals.c.id)
            )
        ).scalar_one()
        await audit(s, ctx.actor, "ai.policy.proposed", "policy_proposal", pid, tenant_id=None, base_version=base)
        return await _proposal(s, pid)


async def _proposal(s: AsyncSession, pid: uuid.UUID, *, lock: bool = False) -> ProposalOut:
    stmt = select(policy_proposals).where(policy_proposals.c.id == pid)
    if lock:
        stmt = stmt.with_for_update()
    row = (await s.execute(stmt)).mappings().first()
    if row is None:
        raise NotFound("Proposal not found")
    return ProposalOut.model_validate(dict(row))


async def _open_proposal(s: AsyncSession, pid: uuid.UUID, wanted: str) -> ProposalOut:
    p = await _proposal(s, pid, lock=True)
    if p.status != wanted:
        raise Conflict(f"Proposal is {p.status}")
    if p.status == "pending" and p.expires_at <= datetime.now(UTC):
        await s.execute(update(policy_proposals).where(policy_proposals.c.id == pid).values(status="expired"))
        raise Conflict("Proposal expired")
    return p


@router.post("/policies/proposals/{proposal_id}/approve", response_model=ProposalOut)
async def approve_proposal(proposal_id: uuid.UUID, body: DecisionIn, request: Request, ctx: Manage, _: StepUp) -> Any:
    approver = _uid(ctx)
    async with db(request).global_session(ctx.actor) as s:
        p = await _open_proposal(s, proposal_id, "pending")
        if p.proposed_by == approver:
            raise Forbidden("A different administrator must approve this policy change")
        await s.execute(
            update(policy_proposals)
            .where(policy_proposals.c.id == proposal_id)
            .values(status="approved", approved_by=approver, decision_note=body.note or None,
                    decided_at=func.now(), updated_at=func.now())
        )  # fmt: skip
        await audit(s, ctx.actor, "ai.policy.approved", "policy_proposal", proposal_id, tenant_id=None)
        return await _proposal(s, proposal_id)


@router.post("/policies/proposals/{proposal_id}/reject", response_model=ProposalOut)
async def reject_proposal(proposal_id: uuid.UUID, body: DecisionIn, request: Request, ctx: Manage, _: StepUp) -> Any:
    async with db(request).global_session(ctx.actor) as s:
        p = await _proposal(s, proposal_id, lock=True)
        if p.status not in ("pending", "approved"):
            raise Conflict(f"Proposal is {p.status}")
        await s.execute(
            update(policy_proposals)
            .where(policy_proposals.c.id == proposal_id)
            .values(status="rejected", rejected_by=_uid(ctx), decision_note=body.note or None,
                    decided_at=func.now(), updated_at=func.now())
        )  # fmt: skip
        await audit(s, ctx.actor, "ai.policy.rejected", "policy_proposal", proposal_id, tenant_id=None)
        return await _proposal(s, proposal_id)


async def _activate(s: AsyncSession, policy: dict[str, Any], actor: str, **extra: Any) -> int:
    # Callers hold the policy advisory lock, so max()+1 cannot race.
    next_version = (await s.execute(select(func.coalesce(func.max(ai_policy_versions.c.version), 0) + 1))).scalar_one()
    await s.execute(
        update(ai_policy_versions).where(ai_policy_versions.c.status == "active").values(status="superseded")
    )
    await s.execute(
        ai_policy_versions.insert().values(
            version=next_version, policy=policy, status="active", deployed_by=actor, **extra
        )
    )
    return int(next_version)


@router.post("/policies/proposals/{proposal_id}/deploy", response_model=ProposalOut)
async def deploy_proposal(proposal_id: uuid.UUID, request: Request, ctx: Manage, _: StepUp) -> Any:
    async with db(request).global_session(ctx.actor) as s:
        await s.execute(select(func.pg_advisory_xact_lock(7_240_311)))  # serialize policy deployments
        p = await _open_proposal(s, proposal_id, "approved")
        active, _policy = await active_policy(s)
        if p.base_version != active:
            raise Conflict("The active policy changed since this proposal was made; propose again")
        policy = ToolPolicy.model_validate(p.policy)  # re-validate at deploy time
        version = await _activate(s, policy.model_dump(), str(ctx.actor), proposal_id=proposal_id)
        await s.execute(
            update(policy_proposals)
            .where(policy_proposals.c.id == proposal_id)
            .values(status="deployed", deployed_by=_uid(ctx), deployed_at=func.now(), deployed_version=version,
                    updated_at=func.now())
        )  # fmt: skip
        await audit(
            s, ctx.actor, "ai.policy.deployed", "ai_policy_version", version, tenant_id=None,
            proposal_id=str(proposal_id), approved_by=str(p.approved_by), previous_version=active,
        )  # fmt: skip
        return await _proposal(s, proposal_id)


@router.post("/policies/versions/{version}/rollback", response_model=PoliciesOut)
async def rollback(version: int, request: Request, ctx: Manage, _: StepUp) -> Any:
    """Re-activate a previously deployed (hence approved) version as a new version. Audited."""
    async with db(request).global_session(ctx.actor) as s:
        await s.execute(select(func.pg_advisory_xact_lock(7_240_311)))
        active, _policy = await active_policy(s)
        row = (
            (await s.execute(select(ai_policy_versions).where(ai_policy_versions.c.version == version)))
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound("Policy version not found")
        if version == active:
            raise Conflict("That version is already active")
        policy = ToolPolicy.model_validate(row["policy"])
        new_version = await _activate(s, policy.model_dump(), str(ctx.actor), rollback_of=version)
        await audit(
            s, ctx.actor, "ai.policy.rolled_back", "ai_policy_version", new_version, tenant_id=None,
            restored_version=version, previous_version=active,
        )  # fmt: skip
    return await list_policies(request, ctx)

"""Tenant AI visibility and platform AI control plane (build.md §24.8)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from ai_gateway.policy import period_now
from ai_gateway.tables import (
    ai_budget_policies,
    ai_kill_switches,
    ai_models,
    ai_requests,
    ai_security_events,
    ai_usage_records,
    outbox,
    routing_policies,
    tenant_model_allowlists,
)
from nexus_core.audit import emit_audit
from nexus_core.authz import AuthContext, require_permission, require_step_up
from nexus_core.errors import NotFound, Unprocessable
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page

tenant_router = APIRouter(prefix="/api/v1/ai", tags=["ai"])
platform_router = APIRouter(prefix="/api/v1/platform/ai", tags=["platform-ai"])
PRODUCER = "ai-gateway"
StepUp = Annotated[AuthContext, Depends(require_step_up())]


def perm(p: str) -> Any:
    return Annotated[AuthContext, Depends(require_permission(p))]


def _row(r: Any) -> dict[str, Any]:
    return {
        k: (str(v) if isinstance(v, uuid.UUID) else v.isoformat() if isinstance(v, datetime) else v)
        for k, v in dict(r).items()
    }


async def _audit(
    request: Request,
    ctx: AuthContext,
    tenant_id: uuid.UUID | None,
    action: str,
    resource_type: str,
    resource_id: Any,
    **meta: Any,
) -> None:
    db = request.app.state.db
    session_cm = db.tenant_session(tenant_id, ctx.actor) if tenant_id else db.global_session(ctx.actor)
    async with session_cm as s:
        await emit_audit(
            s,
            outbox,
            producer=PRODUCER,
            actor=ctx.actor,
            tenant_id=tenant_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=meta,
        )


# ============================================================================ tenant
@tenant_router.get("/models")
async def tenant_models(request: Request, ctx: perm("ai:agents:read")) -> dict[str, Any]:
    async with request.app.state.db.tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = (
            (
                await s.execute(
                    select(
                        ai_models.c.model_id,
                        ai_models.c.display_name,
                        ai_models.c.provider,
                        ai_models.c.purposes,
                        ai_models.c.allowed_classifications,
                    )
                    .where(ai_models.c.enabled.is_(True))
                    .order_by(ai_models.c.model_id)
                )
            )
            .mappings()
            .all()
        )
        allow = set((await s.execute(select(tenant_model_allowlists.c.model_id))).scalars())
    return {"items": [dict(r) | {"platform_restricted": bool(allow) and r["model_id"] not in allow} for r in rows]}


@tenant_router.get("/usage")
async def tenant_usage(request: Request, ctx: perm("ai:usage:read"), period: str | None = None) -> dict[str, Any]:
    period = period or period_now()
    async with request.app.state.db.tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = (await s.execute(select(ai_usage_records).where(ai_usage_records.c.period == period))).mappings().all()
        budgets = (await s.execute(select(ai_budget_policies))).mappings().all()
    total = {
        k: sum(int(r[k]) for r in rows) for k in ("requests", "input_tokens", "output_tokens", "cost_micros", "denied")
    }
    return {
        "period": period,
        "total": total,
        "by_agent": [_row(r) for r in rows],
        "budgets": [_row(b) for b in budgets],
    }


@tenant_router.get("/requests")
async def tenant_requests(
    request: Request, ctx: perm("ai:usage:read"), limit: Limit = 50, cursor: Cursor = None
) -> dict[str, Any]:
    """Metadata only: there is no prompt or completion text to show (OWASP LLM02)."""
    async with request.app.state.db.tenant_session(ctx.require_tenant(), ctx.actor) as s:
        stmt = apply_keyset(select(ai_requests), ai_requests.c.created_at, ai_requests.c.id, cursor, limit)
        rows = [dict(r) for r in (await s.execute(stmt)).mappings().all()]
    page, next_cursor = build_page(rows, limit)
    return {"items": [_row(r) for r in page], "next_cursor": next_cursor}


@tenant_router.get("/security-events")
async def tenant_security_events(
    request: Request, ctx: perm("audit:read"), limit: Limit = 50, cursor: Cursor = None
) -> dict[str, Any]:
    async with request.app.state.db.tenant_session(ctx.require_tenant(), ctx.actor) as s:
        stmt = apply_keyset(
            select(ai_security_events), ai_security_events.c.created_at, ai_security_events.c.id, cursor, limit
        )
        rows = [dict(r) for r in (await s.execute(stmt)).mappings().all()]
    page, next_cursor = build_page(rows, limit)
    return {"items": [_row(r) for r in page], "next_cursor": next_cursor}


class BudgetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: uuid.UUID | None = None
    monthly_limit_usd: int = Field(ge=0, le=1_000_000)
    per_request_limit_usd: float | None = Field(default=None, gt=0, le=100)


@tenant_router.put("/budgets")
async def put_budget(body: BudgetIn, request: Request, ctx: perm("tenant:manage")) -> dict[str, Any]:
    """Tenants can only tighten spend: the effective limit is min(tenant settings, these policies)."""
    tenant_id = ctx.require_tenant()
    values = {
        "tenant_id": tenant_id,
        "scope": "agent" if body.agent_id else "tenant",
        "agent_id": body.agent_id,
        "monthly_limit_micros": body.monthly_limit_usd * 1_000_000,
        "per_request_limit_micros": int(body.per_request_limit_usd * 1_000_000) if body.per_request_limit_usd else None,
        "updated_by": uuid.UUID(ctx.principal.sub),
    }
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as s:
        existing = (
            await s.execute(
                select(ai_budget_policies.c.id).where(
                    ai_budget_policies.c.scope == values["scope"],
                    ai_budget_policies.c.agent_id.is_(None)
                    if body.agent_id is None
                    else ai_budget_policies.c.agent_id == body.agent_id,
                )
            )
        ).scalar_one_or_none()
        if existing:
            await s.execute(
                update(ai_budget_policies)
                .where(ai_budget_policies.c.id == existing)
                .values(
                    monthly_limit_micros=values["monthly_limit_micros"],
                    per_request_limit_micros=values["per_request_limit_micros"],
                    updated_by=values["updated_by"],
                    version=ai_budget_policies.c.version + 1,
                )
            )
        else:
            await s.execute(insert(ai_budget_policies).values(**values))
        await emit_audit(
            s,
            outbox,
            producer=PRODUCER,
            actor=ctx.actor,
            tenant_id=tenant_id,
            action="ai.budget.updated",
            resource_type="ai_budget",
            resource_id=str(body.agent_id or "tenant"),
            metadata={"monthly_limit_usd": body.monthly_limit_usd},
        )
    return {
        "scope": values["scope"],
        "agent_id": str(body.agent_id) if body.agent_id else None,
        "monthly_limit_usd": body.monthly_limit_usd,
        "per_request_limit_usd": body.per_request_limit_usd,
    }


# ============================================================================ platform
@platform_router.get("/models")
async def platform_models(request: Request, ctx: perm("platform:ai:read")) -> dict[str, Any]:
    async with request.app.state.db.global_session(ctx.actor) as s:
        rows = (await s.execute(select(ai_models).order_by(ai_models.c.model_id))).mappings().all()
        routes = (await s.execute(select(routing_policies).order_by(routing_policies.c.purpose))).mappings().all()
    return {"models": [_row(r) for r in rows], "routing": [_row(r) for r in routes]}


class ModelPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    input_price_micros_per_mtok: int | None = Field(default=None, ge=0)
    output_price_micros_per_mtok: int | None = Field(default=None, ge=0)
    allowed_classifications: list[Literal["public", "internal", "confidential", "restricted"]] | None = None


@platform_router.patch("/models/{model_id}")
async def patch_model(
    model_id: str, body: ModelPatch, request: Request, ctx: perm("platform:ai:manage"), _: StepUp
) -> dict[str, Any]:
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise Unprocessable("No changes requested")
    async with request.app.state.db.global_session(ctx.actor) as s:
        result = await s.execute(
            update(ai_models)
            .where(ai_models.c.model_id == model_id)
            .values(**changes, updated_by=uuid.UUID(ctx.principal.sub))
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFound("Model is not registered")
        await emit_audit(
            s,
            outbox,
            producer=PRODUCER,
            actor=ctx.actor,
            tenant_id=None,
            action="ai.model.updated",
            resource_type="ai_model",
            resource_id=model_id,
            metadata={"fields": sorted(changes)},
        )
    return {"model_id": model_id, **changes}


class RouteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    models: list[str] = Field(min_length=1, max_length=5)
    failover_allowed: bool = False


@platform_router.put("/routing/{purpose}")
async def put_route(
    purpose: str, body: RouteIn, request: Request, ctx: perm("platform:ai:manage"), _: StepUp
) -> dict[str, Any]:
    async with request.app.state.db.global_session(ctx.actor) as s:
        known = set(
            (await s.execute(select(ai_models.c.model_id).where(ai_models.c.model_id.in_(body.models)))).scalars()
        )
        if known != set(body.models):
            raise Unprocessable("Routing may only reference registered models")
        result = await s.execute(
            update(routing_policies)
            .where(routing_policies.c.purpose == purpose)
            .values(models=body.models, failover_allowed=body.failover_allowed, updated_by=uuid.UUID(ctx.principal.sub))
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFound("Unknown purpose")
        await emit_audit(
            s,
            outbox,
            producer=PRODUCER,
            actor=ctx.actor,
            tenant_id=None,
            action="ai.routing.updated",
            resource_type="ai_routing",
            resource_id=purpose,
            metadata={"models": body.models, "failover": body.failover_allowed},
        )
    return {"purpose": purpose, **body.model_dump()}


class AllowlistIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    models: list[str] = Field(max_length=20)


@platform_router.put("/tenants/{tenant_id}/models")
async def put_tenant_allowlist(
    tenant_id: uuid.UUID, body: AllowlistIn, request: Request, ctx: perm("platform:ai:manage"), _: StepUp
) -> dict[str, Any]:
    """Empty list = no platform restriction (tenant settings still apply)."""
    db = request.app.state.db
    async with db.global_session(ctx.actor) as s:
        known = set(
            (await s.execute(select(ai_models.c.model_id).where(ai_models.c.model_id.in_(body.models)))).scalars()
        )
    if known != set(body.models):
        raise Unprocessable("Allowlists may only reference registered models")
    async with db.tenant_session(tenant_id, ctx.actor) as s:
        await s.execute(tenant_model_allowlists.delete())
        for m in sorted(set(body.models)):
            await s.execute(
                insert(tenant_model_allowlists).values(
                    tenant_id=tenant_id, model_id=m, created_by=uuid.UUID(ctx.principal.sub)
                )
            )
        await emit_audit(
            s,
            outbox,
            producer=PRODUCER,
            actor=ctx.actor,
            tenant_id=tenant_id,
            action="ai.tenant_allowlist.updated",
            resource_type="ai_allowlist",
            resource_id=str(tenant_id),
            metadata={"models": sorted(set(body.models))},
        )
    return {"tenant_id": str(tenant_id), "models": sorted(set(body.models))}


@platform_router.get("/kill-switches")
async def list_kill_switches(request: Request, ctx: perm("platform:ai:read")) -> dict[str, Any]:
    async with request.app.state.db.global_session(ctx.actor) as s:
        rows = (await s.execute(select(ai_kill_switches).order_by(ai_kill_switches.c.scope))).mappings().all()
    return {"items": [_row(r) for r in rows]}


class KillSwitchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["platform", "provider", "model", "tenant", "agent"]
    target: str = Field(min_length=1, max_length=80, pattern=r"^(\*|[a-z0-9][a-z0-9.-]{1,79})$")
    engaged: bool
    reason: str = Field(min_length=5, max_length=300)


@platform_router.post("/kill-switches")
async def set_kill_switch(
    body: KillSwitchIn, request: Request, ctx: perm("platform:ai:manage"), _: StepUp
) -> dict[str, Any]:
    if (body.scope == "platform") != (body.target == "*"):
        raise Unprocessable("Use target '*' for the platform switch, and only for it")
    if body.scope in ("tenant", "agent"):
        try:
            uuid.UUID(body.target)
        except ValueError as exc:
            raise Unprocessable("Tenant and agent switches target a UUID") from exc
    async with request.app.state.db.global_session(ctx.actor) as s:
        await s.execute(
            insert(ai_kill_switches)
            .values(**body.model_dump(), changed_by=uuid.UUID(ctx.principal.sub))
            .on_conflict_do_update(
                index_elements=["scope", "target"],
                set_={"engaged": body.engaged, "reason": body.reason, "changed_by": uuid.UUID(ctx.principal.sub)},
            )
        )
        await emit_audit(
            s,
            outbox,
            producer=PRODUCER,
            actor=ctx.actor,
            tenant_id=uuid.UUID(body.target) if body.scope == "tenant" else None,
            action="ai.kill_switch." + ("engaged" if body.engaged else "released"),
            resource_type="ai_kill_switch",
            resource_id=f"{body.scope}:{body.target}",
            metadata={"reason": body.reason},
        )
    return body.model_dump()


@platform_router.get("/security-events")
async def platform_security_events(
    request: Request,
    ctx: perm("platform:ai:read"),
    limit: Limit = 50,
    cursor: Cursor = None,
    min_severity: Literal["info", "low", "medium", "high"] = "low",
) -> dict[str, Any]:
    """Cross-tenant view through the dedicated read-only `aigw_platform` role (explicit SELECT policy)."""
    platform_db = getattr(request.app.state, "platform_db", None)
    if platform_db is None:
        raise Unprocessable("The platform read path is not configured")
    ranks = {"info": 0, "low": 1, "medium": 2, "high": 3}
    allowed = [k for k, v in ranks.items() if v >= ranks[min_severity]]
    async with platform_db.engine.connect() as conn:
        stmt = apply_keyset(
            select(ai_security_events).where(ai_security_events.c.severity.in_(allowed)),
            ai_security_events.c.created_at,
            ai_security_events.c.id,
            cursor,
            limit,
        )
        rows = [dict(r) for r in (await conn.execute(stmt)).mappings().all()]
    page, next_cursor = build_page(rows, limit)
    await _audit(
        request,
        ctx,
        None,
        "ai.security_events.viewed",
        "ai_security_events",
        "platform",
        count=len(page),
        min_severity=min_severity,
    )
    return {"items": [_row(r) for r in page], "next_cursor": next_cursor}

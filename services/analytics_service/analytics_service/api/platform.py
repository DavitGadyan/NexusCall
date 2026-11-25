"""Platform usage and cost totals per tenant (`platform:usage:read`, build.md §18.1A "Usage and billing
readiness"). Reads through the read-only `analytics_platform` role with its own SELECT policy; every
query is audited."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from analytics_service.reports import date_range
from analytics_service.schemas import UsageOut
from analytics_service.tables import daily_metrics, outbox
from nexus_core.audit import emit_audit
from nexus_core.authz import AuthContext, require_permission
from nexus_core.db import Database
from nexus_core.errors import ServiceUnavailable

router = APIRouter(prefix="/api/v1/platform", tags=["platform-usage"])
Usage = Annotated[AuthContext, Depends(require_permission("platform:usage:read"))]
USAGE_METRICS = (
    "calls.ended",
    "calls.duration_seconds",
    "calls.cost_micros",
    "emails.accepted",
    "ai.requests",
    "ai.input_tokens",
    "ai.output_tokens",
    "ai.cost_micros",
)


def platform_db(request: Request) -> Database:
    database: Database | None = getattr(request.app.state, "platform_db", None)
    if database is None:
        raise ServiceUnavailable("The platform usage read path is not configured")
    return database


@router.get("/usage", response_model=UsageOut, response_model_by_alias=True)
async def usage(
    request: Request,
    ctx: Usage,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
) -> dict[str, Any]:
    settings = request.app.state.settings
    rng = date_range(from_, to, max_days=settings.max_range_days)
    async with platform_db(request).global_session(ctx.actor) as s:
        rows = (
            await s.execute(
                select(daily_metrics.c.tenant_id, daily_metrics.c.metric, func.sum(daily_metrics.c.value))
                .where(
                    daily_metrics.c.day >= rng.start,
                    daily_metrics.c.day <= rng.end,
                    daily_metrics.c.dimension == "",
                    daily_metrics.c.metric.in_(USAGE_METRICS),
                )
                .group_by(daily_metrics.c.tenant_id, daily_metrics.c.metric)
            )
        ).all()
    per_tenant: dict[uuid.UUID, dict[str, int]] = defaultdict(dict)
    for tenant_id, metric, total in rows:
        per_tenant[tenant_id][metric] = int(total or 0)
    unit = int(settings.email_unit_cost_micros)
    items = []
    for tenant_id, m in sorted(per_tenant.items(), key=lambda kv: str(kv[0])):
        email_cost = m.get("emails.accepted", 0) * unit
        items.append(
            {
                "tenant_id": tenant_id,
                "calls": m.get("calls.ended", 0),
                "call_seconds": m.get("calls.duration_seconds", 0),
                "call_cost_micros": m.get("calls.cost_micros", 0),
                "emails_accepted": m.get("emails.accepted", 0),
                "email_cost_micros": email_cost,
                "ai_requests": m.get("ai.requests", 0),
                "ai_input_tokens": m.get("ai.input_tokens", 0),
                "ai_output_tokens": m.get("ai.output_tokens", 0),
                "ai_cost_micros": m.get("ai.cost_micros", 0),
                "total_cost_micros": m.get("calls.cost_micros", 0) + email_cost + m.get("ai.cost_micros", 0),
            }
        )
    keys = ("calls", "call_seconds", "call_cost_micros", "emails_accepted", "email_cost_micros", "ai_requests",
            "ai_cost_micros", "total_cost_micros")  # fmt: skip
    totals = {"tenants": len(items), **{k: sum(i[k] for i in items) for k in keys}}
    # Audited through the outbox (platform-level event: no tenant context).
    async with request.app.state.db.global_session(ctx.actor) as s:
        await emit_audit(
            s,
            outbox,
            producer="analytics-service",
            actor=ctx.actor,
            tenant_id=None,
            action="platform.usage.viewed",
            resource_type="usage_report",
            metadata={"from": rng.start.isoformat(), "to": rng.end.isoformat(), "tenants": len(items)},
        )
    return {"from": rng.start, "to": rng.end, "email_cost_estimated": True, "items": items, "totals": totals}

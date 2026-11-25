"""Tenant dashboards (`analytics:read`): overview, timeseries and campaign drill-down."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from analytics_service.projections import METRICS
from analytics_service.reports import campaign_report, date_range, metric_totals, overview, timeseries
from analytics_service.schemas import CampaignOut, MetricsOut, OverviewOut, TimeseriesOut
from nexus_core.authz import AuthContext, require_permission
from nexus_core.errors import NotFound, Unprocessable

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])
Read = Annotated[AuthContext, Depends(require_permission("analytics:read"))]
From = Annotated[date | None, Query(alias="from")]
DIMENSION_RE = r"^(|campaign:[0-9a-f-]{36}|purpose:[a-z0-9_]{1,32}|category:[a-z]{1,16}|channel:[a-z]{1,8})$"


@router.get("/overview", response_model=OverviewOut, response_model_by_alias=True)
async def get_overview(request: Request, ctx: Read, from_: From = None, to: date | None = None) -> dict[str, Any]:
    settings = request.app.state.settings
    rng = date_range(from_, to, max_days=settings.max_range_days)
    async with request.app.state.db.tenant_session(ctx.require_tenant(), ctx.actor) as s:
        totals = await metric_totals(s, rng)
    return overview(totals, rng, email_unit_cost_micros=settings.email_unit_cost_micros)


@router.get("/timeseries", response_model=TimeseriesOut, response_model_by_alias=True)
async def get_timeseries(
    request: Request,
    ctx: Read,
    metric: Annotated[str, Query(max_length=64)],
    from_: From = None,
    to: date | None = None,
    dimension: Annotated[str, Query(max_length=128, pattern=DIMENSION_RE)] = "",
) -> dict[str, Any]:
    if metric not in METRICS:
        raise Unprocessable("Unknown metric", extra={"allowed": sorted(METRICS)})
    rng = date_range(from_, to, max_days=request.app.state.settings.max_range_days)
    async with request.app.state.db.tenant_session(ctx.require_tenant(), ctx.actor) as s:
        points = await timeseries(s, metric, rng, dimension)
    return {"metric": metric, "dimension": dimension, "from": rng.start, "to": rng.end, "points": points}


@router.get("/metrics", response_model=MetricsOut)
async def list_metrics(ctx: Read) -> dict[str, Any]:
    return {"items": sorted(METRICS)}


@router.get("/campaigns/{campaign_id}", response_model=CampaignOut)
async def get_campaign(campaign_id: uuid.UUID, request: Request, ctx: Read) -> dict[str, Any]:
    async with request.app.state.db.tenant_session(ctx.require_tenant(), ctx.actor) as s:
        report = await campaign_report(s, campaign_id)
    if report is None:
        raise NotFound("No analytics for this campaign")
    return report

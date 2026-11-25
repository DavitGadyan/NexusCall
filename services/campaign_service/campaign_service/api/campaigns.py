"""Call campaigns: CRUD, lifecycle actions, idempotent launch, report, tenant kill switch."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from campaign_service.domain import (
    AppDeps,
    audit,
    emit,
    kill_switch_engaged,
    load_campaign,
    transition,
)
from campaign_service.schemas import (
    EDITABLE_WHEN_PAUSED,
    ActionIn,
    CampaignContactOut,
    CampaignIn,
    CampaignOut,
    CampaignPage,
    CampaignPatch,
    ContactPage,
    KillSwitchIn,
    KillSwitchOut,
    ReportOut,
    campaign_out,
)
from campaign_service.tables import call_campaigns, campaign_contacts, campaign_events, idempotency_keys, kill_switches
from campaign_service.validation import remote_problems, schedule_problems
from nexus_core.authz import AuthContext, require_permission, require_step_up
from nexus_core.celery_app import JobContext
from nexus_core.clients import UpstreamError, UpstreamUnavailable, service_client
from nexus_core.context import Actor, current_correlation_id
from nexus_core.errors import Conflict, ProblemError, ServiceUnavailable, Unprocessable
from nexus_core.idempotency import IdempotencyStore
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page
from nexus_core.ratelimit import enforce_fixed_window

router = APIRouter(prefix="/api/v1/campaigns", tags=["campaigns"])
Read = Annotated[AuthContext, Depends(require_permission("campaigns:read"))]
Create = Annotated[AuthContext, Depends(require_permission("campaigns:create"))]
Manage = Annotated[AuthContext, Depends(require_permission("campaigns:manage"))]
Launch = Annotated[AuthContext, Depends(require_permission("campaigns:launch"))]
StepUp = Annotated[AuthContext, Depends(require_step_up())]
IDEMPOTENCY = IdempotencyStore(idempotency_keys)
SNAPSHOT_TASK = "campaigns.snapshot"
log = logging.getLogger(__name__)


def _db(request: Request) -> Any:
    return request.app.state.db


def _ctx(tenant_id: uuid.UUID, actor: Actor) -> JobContext:
    return JobContext(
        tenant_id=tenant_id,
        correlation_id=uuid.UUID(current_correlation_id()),
        actor_type=actor.type,
        actor_id=actor.id,
    )


async def _enqueue_snapshot(request: Request, tenant_id: uuid.UUID, actor: Actor, campaign_id: uuid.UUID) -> None:
    try:
        await request.app.state.enqueue(
            SNAPSHOT_TASK, "calls.schedule", _ctx(tenant_id, actor), campaign_id=str(campaign_id)
        )
    except Exception:  # noqa: BLE001 - the dispatcher's sweep re-enqueues pending snapshots
        log.warning("snapshot_enqueue_failed", extra={"campaign_id": str(campaign_id)})


# ------------------------------------------------------------------------------------- kill switch
def _kill_out(row: Any) -> KillSwitchOut:
    if row is None:
        return KillSwitchOut(
            scope="tenant", channel="call", engaged=False, reason=None, changed_by=None, updated_at=None
        )
    return KillSwitchOut(**{k: row[k] for k in KillSwitchOut.model_fields})


@router.get("/kill-switch", response_model=KillSwitchOut)
async def get_kill_switch(request: Request, ctx: Read) -> KillSwitchOut:
    async with _db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        row = (await s.execute(select(kill_switches).where(kill_switches.c.scope == "tenant"))).mappings().first()
    return _kill_out(row)


@router.post("/kill-switch", response_model=KillSwitchOut)
async def set_kill_switch(body: KillSwitchIn, request: Request, ctx: Manage, _: StepUp) -> KillSwitchOut:
    """Stops all call dispatch for the tenant immediately (checked on every dispatch and by call-service)."""
    tenant_id = ctx.require_tenant()
    async with _db(request).tenant_session(tenant_id, ctx.actor) as s:
        row = (
            (
                await s.execute(
                    insert(kill_switches)
                    .values(
                        tenant_id=tenant_id,
                        scope="tenant",
                        channel="call",
                        engaged=body.engaged,
                        reason=body.reason,
                        changed_by=str(ctx.actor),
                    )
                    .on_conflict_do_update(
                        constraint="uq_kill_switches_scope",
                        set_={
                            "engaged": body.engaged,
                            "reason": body.reason,
                            "changed_by": str(ctx.actor),
                            "version": kill_switches.c.version + 1,
                            "updated_at": func.now(),
                        },
                    )
                    .returning(kill_switches)
                )
            )
            .mappings()
            .one()
        )
        await emit(
            s,
            "campaign.kill_switch",
            tenant_id=tenant_id,
            actor=ctx.actor,
            payload={"scope": "tenant", "channel": "call", "engaged": body.engaged},
        )
        await audit(
            s,
            ctx.actor,
            "campaign.kill_switch.engaged" if body.engaged else "campaign.kill_switch.released",
            tenant_id,
            tenant_id=tenant_id,
            resource_type="kill_switch",
        )
    return _kill_out(row)


# ------------------------------------------------------------------------------------------ CRUD
@router.get("", response_model=CampaignPage)
async def list_campaigns(
    request: Request,
    ctx: Read,
    limit: Limit = 50,
    cursor: Cursor = None,
    state: Literal["draft", "scheduled", "running", "paused", "completed", "canceled", "failed"] | None = None,
) -> CampaignPage:
    stmt = select(call_campaigns)
    if state:
        stmt = stmt.where(call_campaigns.c.state == state)
    stmt = apply_keyset(stmt, call_campaigns.c.created_at, call_campaigns.c.id, cursor, limit)
    async with _db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = list((await s.execute(stmt)).mappings().all())
    page, next_cursor = build_page(rows, limit)
    return CampaignPage(items=[campaign_out(r) for r in page], next_cursor=next_cursor)


@router.post("", status_code=201, response_model=CampaignOut)
async def create_campaign(
    body: CampaignIn, request: Request, ctx: Create, idempotency_key: Annotated[str | None, Header()] = None
) -> Any:
    if not idempotency_key:
        raise Unprocessable("This operation requires an Idempotency-Key header")
    tenant_id = ctx.require_tenant()
    async with _db(request).tenant_session(tenant_id, ctx.actor) as s:
        outcome = await IDEMPOTENCY.begin(
            s,
            tenant_id=tenant_id,
            operation="campaigns.create",
            key=idempotency_key,
            payload=body.model_dump(mode="json"),
        )
        if not outcome.is_new:
            return JSONResponse(outcome.response_body, status_code=outcome.response_status or 201)
        values = body.model_dump(exclude_none=True)
        row = (
            (
                await s.execute(
                    insert(call_campaigns)
                    .values(tenant_id=tenant_id, created_by=str(ctx.actor), **values)
                    .returning(call_campaigns)
                )
            )
            .mappings()
            .one()
        )
        await audit(s, ctx.actor, "campaign.created", row["id"], tenant_id=tenant_id)
        out = campaign_out(row).model_dump(mode="json")
        await IDEMPOTENCY.succeed(s, outcome.record_id, status=201, body=out, resource_id=str(row["id"]))
    return out


@router.get("/{campaign_id}", response_model=CampaignOut)
async def get_campaign(campaign_id: uuid.UUID, request: Request, ctx: Read) -> CampaignOut:
    async with _db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        return campaign_out(await load_campaign(s, campaign_id))


@router.patch("/{campaign_id}", response_model=CampaignOut)
async def patch_campaign(campaign_id: uuid.UUID, body: CampaignPatch, request: Request, ctx: Manage) -> CampaignOut:
    tenant_id = ctx.require_tenant()
    changes = {k: getattr(body, k) for k in body.model_fields_set - {"expected_version"}}
    if not changes:
        raise Unprocessable("No changes requested")
    async with _db(request).tenant_session(tenant_id, ctx.actor) as s:
        current = await load_campaign(s, campaign_id, lock=True)
        if current["state"] == "paused":
            if not set(changes) <= EDITABLE_WHEN_PAUSED:
                raise Conflict("Only pacing, window and end time can change while a campaign is paused")
        elif current["state"] != "draft":
            raise Conflict(f"A {current['state']} campaign cannot be edited")
        nullable = {"description", "start_at", "end_at"}
        if any(v is None and k not in nullable for k, v in changes.items()):
            raise Unprocessable("Only description, start_at and end_at can be cleared")
        merged = {**current, **changes}
        if merged["window_start"] >= merged["window_end"]:
            raise Unprocessable("window_start must be before window_end")
        if merged["start_at"] and merged["end_at"] and merged["start_at"] >= merged["end_at"]:
            raise Unprocessable("start_at must be before end_at")
        row = (
            (
                await s.execute(
                    update(call_campaigns)
                    .where(call_campaigns.c.id == campaign_id, call_campaigns.c.version == body.expected_version)
                    .values(**changes, version=call_campaigns.c.version + 1, updated_at=func.now())
                    .returning(call_campaigns)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise Conflict("Campaign was changed by someone else; reload and retry")
        await audit(s, ctx.actor, "campaign.updated", campaign_id, tenant_id=tenant_id, fields=sorted(changes))
    return campaign_out(row)


@router.delete("/{campaign_id}", status_code=204)
async def delete_campaign(campaign_id: uuid.UUID, request: Request, ctx: Manage) -> Response:
    """Only drafts can be deleted; anything that ran keeps its history (cancel instead)."""
    tenant_id = ctx.require_tenant()
    async with _db(request).tenant_session(tenant_id, ctx.actor) as s:
        current = await load_campaign(s, campaign_id, lock=True)
        if current["state"] != "draft":
            raise Conflict("Only draft campaigns can be deleted; cancel it instead")
        await s.execute(call_campaigns.delete().where(call_campaigns.c.id == campaign_id))
        await audit(s, ctx.actor, "campaign.deleted", campaign_id, tenant_id=tenant_id)
    return Response(status_code=204)


# --------------------------------------------------------------------------------------- lifecycle
async def _simple_action(
    request: Request, ctx: AuthContext, campaign_id: uuid.UUID, action: str, body: ActionIn
) -> CampaignOut:
    tenant_id = ctx.require_tenant()
    async with _db(request).tenant_session(tenant_id, ctx.actor) as s:
        current = await load_campaign(s, campaign_id, lock=True)
        if action == "schedule" and (problems := schedule_problems(current)):
            raise Unprocessable("Campaign is not ready to schedule", extra={"problems": problems})
        if action == "resume" and await kill_switch_engaged(s):
            raise Conflict("The tenant call kill switch is engaged")
        updated = await transition(
            s, current, action, actor=ctx.actor, expected_version=body.expected_version, reason=body.reason
        )
        await audit(s, ctx.actor, f"campaign.{action}", campaign_id, tenant_id=tenant_id, to_state=updated["state"])
    return campaign_out(updated)


@router.post("/{campaign_id}/schedule", response_model=CampaignOut)
async def schedule(campaign_id: uuid.UUID, request: Request, ctx: Manage, body: ActionIn | None = None) -> CampaignOut:
    return await _simple_action(request, ctx, campaign_id, "schedule", body or ActionIn())


@router.post("/{campaign_id}/unschedule", response_model=CampaignOut)
async def unschedule(
    campaign_id: uuid.UUID, request: Request, ctx: Manage, body: ActionIn | None = None
) -> CampaignOut:
    return await _simple_action(request, ctx, campaign_id, "unschedule", body or ActionIn())


@router.post("/{campaign_id}/pause", response_model=CampaignOut)
async def pause(campaign_id: uuid.UUID, request: Request, ctx: Manage, body: ActionIn | None = None) -> CampaignOut:
    return await _simple_action(request, ctx, campaign_id, "pause", body or ActionIn())


@router.post("/{campaign_id}/resume", response_model=CampaignOut)
async def resume(campaign_id: uuid.UUID, request: Request, ctx: Manage, body: ActionIn | None = None) -> CampaignOut:
    return await _simple_action(request, ctx, campaign_id, "resume", body or ActionIn())


@router.post("/{campaign_id}/cancel", response_model=CampaignOut)
async def cancel(campaign_id: uuid.UUID, request: Request, ctx: Manage, body: ActionIn | None = None) -> CampaignOut:
    """Cancels the campaign, skips everything not yet called and cancels queued calls through call-service."""
    body = body or ActionIn()
    tenant_id = ctx.require_tenant()
    async with _db(request).tenant_session(tenant_id, ctx.actor) as s:
        current = await load_campaign(s, campaign_id, lock=True)
        updated = await transition(
            s,
            current,
            "cancel",
            actor=ctx.actor,
            expected_version=body.expected_version,
            values={"completed_at": func.now()},
            reason=body.reason,
        )
        in_flight = list(
            (
                await s.execute(
                    select(campaign_contacts.c.last_call_id).where(
                        campaign_contacts.c.campaign_id == campaign_id,
                        campaign_contacts.c.state == "in_flight",
                        campaign_contacts.c.last_call_id.is_not(None),
                    )
                )
            ).scalars()
        )
        await s.execute(
            update(campaign_contacts)
            .where(
                campaign_contacts.c.campaign_id == campaign_id,
                campaign_contacts.c.state.in_(("pending", "retry_wait", "in_flight")),
            )
            .values(
                state="skipped",
                skip_reason="campaign_canceled",
                version=campaign_contacts.c.version + 1,
                updated_at=func.now(),
            )
        )
        await audit(s, ctx.actor, "campaign.cancel", campaign_id, tenant_id=tenant_id, queued_calls=len(in_flight))
    # Best effort; call-service also consumes campaign.canceled and cancels anything still queued.
    calls = service_client(request.app, "call-service")
    for call_id in in_flight:
        try:
            await calls.post_json(
                f"/internal/v1/calls/{call_id}/cancel",
                {"reason": "campaign_canceled"},
                tenant_id=tenant_id,
                actor=ctx.actor,
            )
        except (UpstreamError, UpstreamUnavailable):
            log.warning("call_cancel_failed", extra={"call_id": str(call_id)})
    return campaign_out(updated)


@router.post("/{campaign_id}/launch", status_code=202, response_model=CampaignOut)
async def launch(
    campaign_id: uuid.UUID,
    request: Request,
    ctx: Launch,
    body: ActionIn | None = None,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Any:
    """Validates agent, segment, tenant limits and kill switch, then starts the campaign. The audience
    snapshot runs asynchronously on `calls.schedule`. Concurrent duplicate launches produce one launch."""
    if not idempotency_key:
        raise Unprocessable("This operation requires an Idempotency-Key header")
    body = body or ActionIn()
    tenant_id = ctx.require_tenant()
    db = _db(request)
    await enforce_fixed_window(
        getattr(request.app.state, "redis", None),
        f"rl:campaign-launch:{tenant_id}",
        request.app.state.settings.launches_per_hour,
        3600,
        "Too many campaign launches; try again later",
    )
    async with db.tenant_session(tenant_id, ctx.actor) as s:
        outcome = await IDEMPOTENCY.begin(
            s,
            tenant_id=tenant_id,
            operation=f"campaigns.launch:{campaign_id}",
            key=idempotency_key,
            payload={"campaign_id": str(campaign_id), "expected_version": body.expected_version},
        )
        if not outcome.is_new:
            return JSONResponse(outcome.response_body, status_code=outcome.response_status or 202)
        campaign = await load_campaign(s, campaign_id)
    try:
        if campaign["state"] != "scheduled":
            raise Conflict(f"Only scheduled campaigns can be launched (state: {campaign['state']})")
        try:
            problems = await remote_problems(AppDeps(request.app), tenant_id, ctx.actor, campaign)
        except (UpstreamError, UpstreamUnavailable) as exc:
            raise ServiceUnavailable("A dependency needed to validate the launch is unavailable") from exc
        async with db.tenant_session(tenant_id, ctx.actor) as s:
            if await kill_switch_engaged(s):
                problems.append("kill_switch_engaged")
        if problems:
            raise Unprocessable("Campaign cannot be launched", extra={"problems": problems})
        async with db.tenant_session(tenant_id, ctx.actor) as s:
            fresh = await load_campaign(s, campaign_id, lock=True)
            updated = await transition(
                s,
                fresh,
                "launch",
                actor=ctx.actor,
                expected_version=body.expected_version,
                values={
                    "launched_at": func.now(),
                    "launched_by": str(ctx.actor),
                    "snapshot_status": "pending",
                    "snapshot_cursor": None,
                    "snapshot_updated_at": func.now(),
                    "pacing_tokens": None,
                    "pacing_refilled_at": None,
                },
            )
            await audit(s, ctx.actor, "campaign.launched", campaign_id, tenant_id=tenant_id)
            out = campaign_out(updated).model_dump(mode="json")
            await IDEMPOTENCY.succeed(s, outcome.record_id, status=202, body=out, resource_id=str(campaign_id))
    except Exception as exc:
        # The key becomes retryable; the refused/failed attempt is audited.
        denied = isinstance(exc, ProblemError) and exc.status < 500
        problems = exc.extra.get("problems", [exc.code]) if isinstance(exc, ProblemError) else ["internal_error"]
        async with db.tenant_session(tenant_id, ctx.actor) as s:
            await IDEMPOTENCY.fail_retryable(s, outcome.record_id)
            await audit(
                s,
                ctx.actor,
                "campaign.launched",
                campaign_id,
                tenant_id=tenant_id,
                outcome="denied" if denied else "failure",
                problems=problems,
            )
        raise
    await _enqueue_snapshot(request, tenant_id, ctx.actor, campaign_id)
    return out


# ----------------------------------------------------------------------------------- reporting
@router.get("/{campaign_id}/report", response_model=ReportOut)
async def report(campaign_id: uuid.UUID, request: Request, ctx: Read) -> ReportOut:
    async with _db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        campaign = await load_campaign(s, campaign_id)
        by_state = dict(
            (
                await s.execute(
                    select(campaign_contacts.c.state, func.count())
                    .where(campaign_contacts.c.campaign_id == campaign_id)
                    .group_by(campaign_contacts.c.state)
                )
            ).all()
        )
        outcomes = dict(
            (
                await s.execute(
                    select(campaign_contacts.c.outcome, func.count())
                    .where(campaign_contacts.c.campaign_id == campaign_id, campaign_contacts.c.outcome.is_not(None))
                    .group_by(campaign_contacts.c.outcome)
                )
            ).all()
        )
        attempts = (
            await s.execute(
                select(func.coalesce(func.sum(campaign_contacts.c.attempts), 0)).where(
                    campaign_contacts.c.campaign_id == campaign_id
                )
            )
        ).scalar_one()
        dispatched = (
            await s.execute(
                select(func.count()).where(
                    campaign_events.c.campaign_id == campaign_id, campaign_events.c.kind == "call_dispatched"
                )
            )
        ).scalar_one()
    return ReportOut(
        campaign_id=campaign_id,
        state=campaign["state"],
        snapshot_status=campaign["snapshot_status"],
        audience=sum(by_state.values()),
        contacts_by_state={k: int(v) for k, v in by_state.items()},
        outcomes={k: int(v) for k, v in outcomes.items()},
        attempts=int(attempts),
        calls_dispatched=int(dispatched),
    )


@router.get("/{campaign_id}/contacts", response_model=ContactPage)
async def list_campaign_contacts(
    campaign_id: uuid.UUID,
    request: Request,
    ctx: Read,
    limit: Limit = 50,
    cursor: Cursor = None,
    state: Literal["pending", "in_flight", "completed", "failed", "skipped", "retry_wait"] | None = None,
) -> ContactPage:
    stmt = select(campaign_contacts).where(campaign_contacts.c.campaign_id == campaign_id)
    if state:
        stmt = stmt.where(campaign_contacts.c.state == state)
    stmt = apply_keyset(stmt, campaign_contacts.c.created_at, campaign_contacts.c.id, cursor, limit)
    async with _db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        await load_campaign(s, campaign_id)
        rows = list((await s.execute(stmt)).mappings().all())
    page, next_cursor = build_page(rows, limit)
    return ContactPage(
        items=[CampaignContactOut(**{k: r[k] for k in CampaignContactOut.model_fields}) for r in page],
        next_cursor=next_cursor,
    )

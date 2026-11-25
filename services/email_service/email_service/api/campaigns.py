"""Email campaigns: CRUD, schedule/launch (idempotent, audited), pause, resume, cancel, report.

Reads accept `emails:campaigns:manage` or `campaigns:read`; every change needs `emails:campaigns:manage`,
checked when the request executes. Sending happens in the scheduler and the worker, never here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import available_timezones

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from email_service.api.common import (
    CampaignReader,
    IdemHeader,
    In,
    Manage,
    Page,
    db,
    idem_begin,
    idem_peek,
    iso,
    require_key,
)
from email_service.api.domains import load_sender
from email_service.api.templates import load_template
from email_service.deps import audit
from email_service.state_machine import CAMPAIGN_TERMINAL, SENT_STATES, InvalidTransition, campaign_next
from email_service.tables import campaign_schedule_index, email_campaigns, email_messages
from nexus_core.clients import UpstreamError, UpstreamUnavailable, service_client, tenant_settings_section
from nexus_core.errors import Conflict, NotFound, ServiceUnavailable, Unprocessable
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page

router = APIRouter(prefix="/api/v1/email/campaigns", tags=["email-campaigns"])
_TZ = frozenset(available_timezones())


class CampaignIn(In):
    name: str = Field(min_length=1, max_length=200)
    sender_id: uuid.UUID
    template_id: uuid.UUID
    segment_id: uuid.UUID
    timezone: str = "UTC"
    max_per_minute: int = Field(default=60, ge=1, le=10000)
    ends_at: datetime | None = None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        if v not in _TZ:
            raise ValueError("unknown IANA timezone")
        return v


class CampaignPatch(In):
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    sender_id: uuid.UUID | None = None
    template_id: uuid.UUID | None = None
    segment_id: uuid.UUID | None = None
    timezone: str | None = None
    max_per_minute: int | None = Field(default=None, ge=1, le=10000)
    ends_at: datetime | None = None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        if v is not None and v not in _TZ:
            raise ValueError("unknown IANA timezone")
        return v


class ScheduleIn(In):
    scheduled_at: datetime | None = None


class ActionIn(In):
    reason: str | None = Field(default=None, max_length=200)


class CampaignOut(BaseModel):
    id: uuid.UUID
    name: str
    state: str
    sender_id: uuid.UUID
    template_id: uuid.UUID
    template_version: int | None
    segment_id: uuid.UUID
    timezone: str
    max_per_minute: int
    scheduled_at: str | None
    ends_at: str | None
    launched_at: str | None
    started_at: str | None
    completed_at: str | None
    pause_reason: str | None
    last_error: str | None
    created_at: str
    updated_at: str
    version: int


class CampaignList(Page):
    items: list[CampaignOut]


class ReportOut(BaseModel):
    campaign_id: uuid.UUID
    state: str
    pause_reason: str | None
    audience_complete: bool
    counts: dict[str, int]
    recipients: int
    sent: int
    rates: dict[str, float]


def _out(row: Any) -> CampaignOut:
    return CampaignOut(
        id=row["id"],
        name=row["name"],
        state=row["state"],
        sender_id=row["sender_id"],
        template_id=row["template_id"],
        template_version=row["template_version"],
        segment_id=row["segment_id"],
        timezone=row["timezone"],
        max_per_minute=row["max_per_minute"],
        scheduled_at=iso(row["scheduled_at"]),
        ends_at=iso(row["ends_at"]),
        launched_at=iso(row["launched_at"]),
        started_at=iso(row["started_at"]),
        completed_at=iso(row["completed_at"]),
        pause_reason=row["pause_reason"],
        last_error=row["last_error"],
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
        version=row["version"],
    )


async def load_campaign(session: Any, campaign_id: uuid.UUID, *, lock: bool = False) -> Any:
    stmt = select(email_campaigns).where(email_campaigns.c.id == campaign_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).mappings().first()
    if row is None:
        raise NotFound("Campaign not found")
    return row


async def _check_refs(session: Any, sender_id: uuid.UUID, template_id: uuid.UUID) -> tuple[Any, Any]:
    sender = await load_sender(session, sender_id)
    template = await load_template(session, template_id)
    if template["category"] != "campaign":
        raise Unprocessable("Campaigns need a campaign template (transactional templates have no unsubscribe link)")
    return sender, template


@router.get("", response_model=CampaignList)
async def list_campaigns(
    request: Request, ctx: CampaignReader, state: str | None = None, limit: Limit = 50, cursor: Cursor = None
) -> CampaignList:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        stmt = select(email_campaigns)
        if state:
            stmt = stmt.where(email_campaigns.c.state == state)
        stmt = apply_keyset(stmt, email_campaigns.c.created_at, email_campaigns.c.id, cursor, limit)
        rows = [dict(r) for r in (await s.execute(stmt)).mappings().all()]
    page, nxt = build_page(rows, limit)
    return CampaignList(items=[_out(r) for r in page], next_cursor=nxt)


@router.post("", status_code=201, response_model=CampaignOut)
async def create_campaign(body: CampaignIn, request: Request, ctx: Manage, idempotency_key: IdemHeader = None) -> Any:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        op = await idem_begin(s, tenant_id=tenant_id, operation="campaigns.create", key=idempotency_key,
                              payload=body.model_dump(mode="json"))  # fmt: skip
        if op.replay is not None:
            return op.replay
        await _check_refs(s, body.sender_id, body.template_id)
        row = (
            (
                await s.execute(
                    insert(email_campaigns)
                    .values(tenant_id=tenant_id, created_by=str(ctx.actor), **body.model_dump())
                    .returning(email_campaigns)
                )
            )
            .mappings()
            .one()
        )
        await audit(s, ctx.actor, "email_campaign.created", "email_campaign", row["id"], tenant_id=tenant_id)
        return await op.done(201, _out(row).model_dump(mode="json"), row["id"])


@router.get("/{campaign_id}", response_model=CampaignOut)
async def get_campaign(campaign_id: uuid.UUID, request: Request, ctx: CampaignReader) -> CampaignOut:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        return _out(await load_campaign(s, campaign_id))


@router.patch("/{campaign_id}", response_model=CampaignOut)
async def patch_campaign(campaign_id: uuid.UUID, body: CampaignPatch, request: Request, ctx: Manage) -> CampaignOut:
    tenant_id = ctx.require_tenant()
    changes = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        current = await load_campaign(s, campaign_id)
        if current["state"] != "draft":
            raise Conflict("Only draft campaigns can be edited")
        await _check_refs(s, changes.get("sender_id", current["sender_id"]),
                          changes.get("template_id", current["template_id"]))  # fmt: skip
        row = (
            (
                await s.execute(
                    update(email_campaigns)
                    .where(
                        email_campaigns.c.id == campaign_id,
                        email_campaigns.c.version == body.expected_version,
                        email_campaigns.c.state == "draft",
                    )
                    .values(**changes, version=email_campaigns.c.version + 1, updated_at=func.now())
                    .returning(email_campaigns)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise Conflict("Campaign was changed by someone else; reload and retry")
        await audit(s, ctx.actor, "email_campaign.updated", "email_campaign", campaign_id, tenant_id=tenant_id,
                    fields=sorted(changes))  # fmt: skip
    return _out(row)


async def _schedule(campaign_id: uuid.UUID, body: ScheduleIn, request: Request, ctx: Any, key: str) -> Any:
    """Launch: draft -> scheduled. Idempotent (Idempotency-Key) and audited. The scheduler starts sending at
    `scheduled_at` (default now), inside the tenant's email window, while the tenant's kill switch is off."""
    tenant_id = ctx.require_tenant()
    now = datetime.now(UTC)
    scheduled_at = body.scheduled_at or now
    if scheduled_at.tzinfo is None:
        raise Unprocessable("scheduled_at must include a timezone offset")
    operation = f"campaigns.launch:{campaign_id}"
    payload = body.model_dump(mode="json")
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        replay = await idem_peek(s, tenant_id=tenant_id, operation=operation, key=key, payload=payload)
        if replay is not None:
            return replay
        camp = await load_campaign(s, campaign_id)
        try:
            campaign_next(camp["state"], "scheduled")
        except InvalidTransition as exc:
            raise Conflict(f"Campaign is {camp['state']} and cannot be launched") from exc
        if camp["ends_at"] and camp["ends_at"] <= scheduled_at:
            raise Unprocessable("ends_at must be after the scheduled start")
        sender, template = await _check_refs(s, camp["sender_id"], camp["template_id"])
        if sender["status"] != "active":
            raise Conflict("Sender identity is disabled")
        if sender["domain_status"] != "verified":
            raise Conflict("Sender domain is not verified", extra={"code_detail": "sender_domain_not_verified"})
    # Fresh external checks (outside the row lock): kill switches and the segment.
    client = service_client(request.app, "tenant-service")
    try:
        comm = await tenant_settings_section(client, getattr(request.app.state, "redis", None), tenant_id,
                                             "communication")  # fmt: skip
        await service_client(request.app, "crm-service").post_json(
            f"/internal/v1/segments/{camp['segment_id']}/resolve",
            {"limit": 1, "channel": "email"},
            tenant_id=tenant_id,
            actor=ctx.actor,
        )
    except UpstreamError as exc:
        if exc.is_not_found:
            raise Unprocessable("Segment not found") from exc
        raise ServiceUnavailable("A dependency is unavailable; retry with the same Idempotency-Key") from exc
    except UpstreamUnavailable as exc:
        raise ServiceUnavailable("A dependency is unavailable; retry with the same Idempotency-Key") from exc
    if not comm.get("effective_emails_enabled", False):
        raise Conflict("Email sending is disabled for this tenant", extra={"code_detail": "emails_disabled"})
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        # The key row is written with the state change, so concurrent duplicates produce one launch.
        op = await idem_begin(s, tenant_id=tenant_id, operation=operation, key=key, payload=payload)
        if op.replay is not None:
            return op.replay
        row = (
            (
                await s.execute(
                    update(email_campaigns)
                    .where(email_campaigns.c.id == campaign_id, email_campaigns.c.state == "draft")
                    .values(
                        state="scheduled",
                        scheduled_at=scheduled_at,
                        launched_at=func.now(),
                        launched_by=str(ctx.actor),
                        template_version=template["current_version"],
                        version=email_campaigns.c.version + 1,
                        updated_at=func.now(),
                    )
                    .returning(email_campaigns)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise Conflict("Campaign was launched or changed concurrently")
        await s.execute(
            insert(campaign_schedule_index)
            .values(campaign_id=campaign_id, tenant_id=tenant_id)
            .on_conflict_do_nothing()
        )
        await audit(
            s, ctx.actor, "email_campaign.launched", "email_campaign", campaign_id, tenant_id=tenant_id,
            template_version=template["current_version"], scheduled_at=scheduled_at.isoformat(),
        )  # fmt: skip
        return await op.done(202, _out(row).model_dump(mode="json"), campaign_id)


@router.post("/{campaign_id}/schedule", status_code=202, response_model=CampaignOut)
async def schedule(
    campaign_id: uuid.UUID, body: ScheduleIn, request: Request, ctx: Manage, idempotency_key: IdemHeader = None
) -> Any:
    return await _schedule(campaign_id, body, request, ctx, require_key(idempotency_key))


@router.post("/{campaign_id}/launch", status_code=202, response_model=CampaignOut)
async def launch(campaign_id: uuid.UUID, request: Request, ctx: Manage, idempotency_key: IdemHeader = None) -> Any:
    """Launch now (same as `schedule` without a start time)."""
    return await _schedule(campaign_id, ScheduleIn(), request, ctx, require_key(idempotency_key))


async def change_state(
    session: Any, *, tenant_id: uuid.UUID, campaign: Any, target: str, values: dict[str, Any] | None = None
) -> Any:
    try:
        campaign_next(campaign["state"], target)
    except InvalidTransition as exc:
        raise Conflict(f"Campaign is {campaign['state']} and cannot become {target}") from exc
    row = (
        (
            await session.execute(
                update(email_campaigns)
                .where(email_campaigns.c.id == campaign["id"], email_campaigns.c.state == campaign["state"])
                .values(state=target, version=email_campaigns.c.version + 1, updated_at=func.now(), **(values or {}))
                .returning(email_campaigns)
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise Conflict("Campaign changed concurrently; reload and retry")
    if target in CAMPAIGN_TERMINAL:
        await session.execute(
            campaign_schedule_index.delete().where(campaign_schedule_index.c.campaign_id == row["id"])
        )
    return row


@router.post("/{campaign_id}/pause", response_model=CampaignOut)
async def pause(campaign_id: uuid.UUID, body: ActionIn, request: Request, ctx: Manage) -> CampaignOut:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        camp = await load_campaign(s, campaign_id, lock=True)
        row = await change_state(s, tenant_id=tenant_id, campaign=camp, target="paused",
                                 values={"pause_reason": "manual"})  # fmt: skip
        await audit(s, ctx.actor, "email_campaign.paused", "email_campaign", campaign_id, tenant_id=tenant_id)
    return _out(row)


@router.post("/{campaign_id}/resume", response_model=CampaignOut)
async def resume(campaign_id: uuid.UUID, body: ActionIn, request: Request, ctx: Manage) -> CampaignOut:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        camp = await load_campaign(s, campaign_id, lock=True)
        if camp["state"] != "paused":
            raise Conflict(f"Campaign is {camp['state']} and cannot be resumed")
        target = "running" if camp["started_at"] else "scheduled"
        row = await change_state(s, tenant_id=tenant_id, campaign=camp, target=target, values={"pause_reason": None})
        await s.execute(
            insert(campaign_schedule_index)
            .values(campaign_id=campaign_id, tenant_id=tenant_id)
            .on_conflict_do_nothing()
        )
        await audit(s, ctx.actor, "email_campaign.resumed", "email_campaign", campaign_id, tenant_id=tenant_id,
                    previous_pause_reason=camp["pause_reason"])  # fmt: skip
    return _out(row)


@router.post("/{campaign_id}/cancel", response_model=CampaignOut)
async def cancel(campaign_id: uuid.UUID, body: ActionIn, request: Request, ctx: Manage) -> CampaignOut:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        camp = await load_campaign(s, campaign_id, lock=True)
        row = await change_state(s, tenant_id=tenant_id, campaign=camp, target="canceled",
                                 values={"completed_at": func.now()})  # fmt: skip
        # Unsent recipients are closed out; a message already handed to the provider keeps its own lifecycle.
        await s.execute(
            update(email_messages)
            .where(email_messages.c.campaign_id == campaign_id, email_messages.c.state.in_(("pending", "queued")))
            .values(state="failed", last_error="campaign_canceled", state_changed_at=func.now(), updated_at=func.now())
        )
        await audit(s, ctx.actor, "email_campaign.canceled", "email_campaign", campaign_id, tenant_id=tenant_id)
    return _out(row)


@router.get("/{campaign_id}/report", response_model=ReportOut)
async def report(campaign_id: uuid.UUID, request: Request, ctx: CampaignReader) -> ReportOut:
    """Provider-reported outcomes only. Opens and clicks are not tracked (unreliable and privacy-invasive)."""
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        camp = await load_campaign(s, campaign_id)
        rows = (
            await s.execute(
                select(email_messages.c.state, func.count())
                .where(email_messages.c.campaign_id == campaign_id)
                .group_by(email_messages.c.state)
            )
        ).all()
    counts = {state: int(n) for state, n in rows}
    sent = sum(counts.get(st, 0) for st in SENT_STATES)

    def rate(state: str) -> float:
        return round(counts.get(state, 0) / sent, 4) if sent else 0.0

    return ReportOut(
        campaign_id=campaign_id,
        state=camp["state"],
        pause_reason=camp["pause_reason"],
        audience_complete=bool(camp["snapshot_done"]),
        counts=counts,
        recipients=sum(counts.values()),
        sent=sent,
        rates={
            "delivery_rate": rate("delivered"),
            "bounce_rate": rate("bounced"),
            "complaint_rate": rate("complained"),
            "unsubscribe_rate": rate("unsubscribed"),
        },
    )

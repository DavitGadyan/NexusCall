"""Read-only views of outbound messages and daily usage. Recipient addresses are masked."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from email_service.api.common import CampaignReader, Page, db, iso
from email_service.security import mask_address
from email_service.tables import email_messages, usage_counters
from nexus_core.clients import UpstreamError, UpstreamUnavailable, service_client, tenant_settings_section
from nexus_core.errors import NotFound
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page

router = APIRouter(prefix="/api/v1/email", tags=["email-messages"])


class MessageOut(BaseModel):
    id: uuid.UUID
    category: str
    state: str
    campaign_id: uuid.UUID | None
    contact_id: uuid.UUID | None
    to_masked: str | None
    from_address: str
    provider_message_id: str | None
    rfc_message_id: str
    attempts: int
    last_error: str | None
    queued_at: str | None
    accepted_at: str | None
    delivered_at: str | None
    state_changed_at: str
    created_at: str


class MessageList(Page):
    items: list[MessageOut]


class UsageDay(BaseModel):
    day: str
    sent: int


class UsageOut(BaseModel):
    daily_quota: int | None
    emails_enabled: bool | None
    days: list[UsageDay]


def _out(row: Any) -> MessageOut:
    return MessageOut(
        id=row["id"],
        category=row["category"],
        state=row["state"],
        campaign_id=row["campaign_id"],
        contact_id=row["contact_id"],
        to_masked=mask_address(row["to_address"]),
        from_address=row["from_address"],
        provider_message_id=row["provider_message_id"],
        rfc_message_id=row["rfc_message_id"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        queued_at=iso(row["queued_at"]),
        accepted_at=iso(row["accepted_at"]),
        delivered_at=iso(row["delivered_at"]),
        state_changed_at=row["state_changed_at"].isoformat(),
        created_at=row["created_at"].isoformat(),
    )


@router.get("/messages", response_model=MessageList)
async def list_messages(
    request: Request,
    ctx: CampaignReader,
    campaign_id: uuid.UUID | None = None,
    state: Annotated[str | None, Query(max_length=16)] = None,
    category: Annotated[str | None, Query(pattern=r"^(campaign|transactional|reply)$")] = None,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> MessageList:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        stmt = select(email_messages)
        if campaign_id:
            stmt = stmt.where(email_messages.c.campaign_id == campaign_id)
        if state:
            stmt = stmt.where(email_messages.c.state == state)
        if category:
            stmt = stmt.where(email_messages.c.category == category)
        stmt = apply_keyset(stmt, email_messages.c.created_at, email_messages.c.id, cursor, limit)
        rows = [dict(r) for r in (await s.execute(stmt)).mappings().all()]
    page, nxt = build_page(rows, limit)
    return MessageList(items=[_out(r) for r in page], next_cursor=nxt)


@router.get("/messages/{message_id}", response_model=MessageOut)
async def get_message(message_id: uuid.UUID, request: Request, ctx: CampaignReader) -> MessageOut:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        row = (await s.execute(select(email_messages).where(email_messages.c.id == message_id))).mappings().first()
    if row is None:
        raise NotFound("Message not found")
    return _out(row)


@router.get("/usage", response_model=UsageOut)
async def usage(request: Request, ctx: CampaignReader, days: Annotated[int, Query(ge=1, le=90)] = 14) -> UsageOut:
    tenant_id = ctx.require_tenant()
    since = datetime.now(UTC).date() - timedelta(days=days - 1)
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        rows = (
            await s.execute(
                select(usage_counters.c.day, usage_counters.c.sent)
                .where(usage_counters.c.day >= since)
                .order_by(usage_counters.c.day.desc())
            )
        ).all()
    quota: int | None = None
    enabled: bool | None = None
    try:
        comm = await tenant_settings_section(service_client(request.app, "tenant-service"),
                                             getattr(request.app.state, "redis", None), tenant_id,
                                             "communication")  # fmt: skip
        quota, enabled = int(comm.get("email_daily_quota", 0)), bool(comm.get("effective_emails_enabled"))
    except (UpstreamError, UpstreamUnavailable):
        pass
    return UsageOut(daily_quota=quota, emails_enabled=enabled,
                    days=[UsageDay(day=d.isoformat(), sent=n) for d, n in rows])  # fmt: skip

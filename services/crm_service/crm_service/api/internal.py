"""Service-to-service API, exactly as BUILD_CONVENTIONS §5 (crm-service). Allow-listed callers only.

The caller asserts the tenant (X-Tenant-ID); every resource is loaded inside that tenant's RLS session, so a
wrong tenant simply finds nothing (404)."""

from __future__ import annotations

import base64
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert

from crm_service import idem
from crm_service.api.segments import load_segment, segment_clause
from crm_service.contactability import contactable_clause, evaluate
from crm_service.deps import db
from crm_service.domain import add_suppression, load_contact, tag_names
from crm_service.normalize import InvalidAddress, mask_email, mask_phone, normalize_email, normalize_phone, value_hash
from crm_service.schemas import In
from crm_service.tables import contact_notes, contacts, suppressions
from nexus_core.authz import InternalContext, require_internal
from nexus_core.errors import Unprocessable

router = APIRouter(prefix="/internal/v1", tags=["internal"], include_in_schema=False)

TOOL_EXECUTION = "svc-tool-execution"
VOICE = "svc-voice-service"
CALL = "svc-call-service"
EMAIL = "svc-email-service"
EMAIL_INBOX = "svc-email-inbox-service"
AI_AGENT = "svc-ai-agent-service"
CAMPAIGN = "svc-campaign-service"

ContactReader = Annotated[
    InternalContext, Depends(require_internal(TOOL_EXECUTION, VOICE, CALL, EMAIL_INBOX, AI_AGENT))
]
ContactabilityReader = Annotated[InternalContext, Depends(require_internal(CALL, EMAIL, CAMPAIGN))]
LookupReader = Annotated[InternalContext, Depends(require_internal(CALL, EMAIL_INBOX))]
BatchReader = Annotated[InternalContext, Depends(require_internal(EMAIL, CAMPAIGN))]
SegmentResolver = Annotated[InternalContext, Depends(require_internal(CAMPAIGN, EMAIL))]
NoteWriter = Annotated[InternalContext, Depends(require_internal(TOOL_EXECUTION))]
SuppressionWriter = Annotated[InternalContext, Depends(require_internal(CALL, VOICE, EMAIL, EMAIL_INBOX))]
SuppressionChecker = Annotated[InternalContext, Depends(require_internal(EMAIL, EMAIL_INBOX, CALL))]


class SourceRef(In):
    type: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class InternalContact(BaseModel):
    contact_id: uuid.UUID
    first_name: str | None
    last_name: str | None
    company: str | None
    timezone: str | None
    locale: str | None
    status: str
    tags: list[str]
    email_masked: str | None
    phone_masked: str | None
    custom_fields: dict[str, Any]


class Contactability(BaseModel):
    contact_id: uuid.UUID
    allowed: bool
    reasons: list[str]
    phone_e164: str | None = None
    email: str | None = None
    timezone: str | None = None
    country: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class LookupOut(BaseModel):
    contact_id: uuid.UUID | None


class BatchIn(In):
    contact_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    channel: Literal["phone", "email"]


class BatchItem(BaseModel):
    contact_id: uuid.UUID
    allowed: bool
    reasons: list[str]
    email: str | None = None
    phone_e164: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    timezone: str | None = None


class BatchOut(BaseModel):
    items: list[BatchItem]


class ResolveIn(In):
    cursor: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=500, ge=1, le=500)
    channel: Literal["phone", "email"]


class ResolvedContact(BaseModel):
    contact_id: uuid.UUID


class ResolveOut(BaseModel):
    items: list[ResolvedContact]
    next_cursor: str | None


class NoteIn(In):
    text: str = Field(min_length=1, max_length=2000)
    source: SourceRef


class NoteOut(BaseModel):
    note_id: uuid.UUID


class SuppressionIn(In):
    channel: Literal["phone", "email", "all"]
    contact_id: uuid.UUID | None = None
    value: str | None = Field(default=None, min_length=3, max_length=320)
    reason: Literal["opt_out", "bounce", "complaint", "manual", "legal"]
    source: SourceRef


class SuppressionOut(BaseModel):
    suppression_id: uuid.UUID


class CheckOut(BaseModel):
    suppressed: bool
    reasons: list[str]


def _encode(contact_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(contact_id.bytes).decode().rstrip("=")


def _decode(cursor: str) -> uuid.UUID:
    try:
        return uuid.UUID(bytes=base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except ValueError as exc:
        raise Unprocessable("Invalid cursor") from exc


@router.get("/contacts/lookup", response_model=LookupOut)
async def lookup(
    request: Request,
    ictx: LookupReader,
    phone: Annotated[str | None, Query(max_length=32)] = None,
    email: Annotated[str | None, Query(max_length=320)] = None,
) -> LookupOut:
    if (phone is None) == (email is None):
        raise Unprocessable("Provide exactly one of phone or email")
    try:
        col, value = (
            (contacts.c.phone_e164, normalize_phone(phone))
            if phone
            else (contacts.c.email, normalize_email(email or ""))
        )
    except InvalidAddress:
        return LookupOut(contact_id=None)
    async with db(request).tenant_session(ictx.require_tenant(), ictx.actor) as s:
        found = (
            await s.execute(select(contacts.c.id).where(col == value, contacts.c.status != "archived"))
        ).scalar_one_or_none()
    return LookupOut(contact_id=found)


@router.post("/contacts/batch", response_model=BatchOut)
async def batch(body: BatchIn, request: Request, ictx: BatchReader) -> BatchOut:
    """Unknown ids are omitted from the response."""
    async with db(request).tenant_session(ictx.require_tenant(), ictx.actor) as s:
        rows = [
            dict(r) for r in (await s.execute(select(contacts).where(contacts.c.id.in_(body.contact_ids)))).mappings()
        ]
        reasons = await evaluate(s, rows, body.channel)
    items = []
    for r in rows:
        allowed = not reasons[r["id"]]
        item = BatchItem(contact_id=r["id"], allowed=allowed, reasons=reasons[r["id"]])
        if allowed:
            item.first_name, item.last_name, item.timezone = r["first_name"], r["last_name"], r["timezone"]
            if body.channel == "email":
                item.email = r["email"]
            else:
                item.phone_e164 = r["phone_e164"]
        items.append(item)
    return BatchOut(items=items)


@router.get("/contacts/{contact_id}", response_model=InternalContact)
async def get_contact(contact_id: uuid.UUID, request: Request, ictx: ContactReader) -> InternalContact:
    async with db(request).tenant_session(ictx.require_tenant(), ictx.actor) as s:
        row = await load_contact(s, contact_id)
        names = (await tag_names(s, [contact_id]))[contact_id]
    return InternalContact(
        contact_id=row["id"],
        first_name=row["first_name"],
        last_name=row["last_name"],
        company=row["company"],
        timezone=row["timezone"],
        locale=row["locale"],
        status=row["status"],
        tags=names,
        email_masked=mask_email(row["email"]),
        phone_masked=mask_phone(row["phone_e164"]),
        custom_fields=row["custom_fields"],
    )


@router.get("/contacts/{contact_id}/contactability", response_model=Contactability)
async def contact_contactability(
    contact_id: uuid.UUID, request: Request, ictx: ContactabilityReader, channel: Literal["phone", "email"]
) -> Contactability:
    async with db(request).tenant_session(ictx.require_tenant(), ictx.actor) as s:
        row = await load_contact(s, contact_id)
        reasons = (await evaluate(s, [row], channel))[contact_id]
    out = Contactability(contact_id=contact_id, allowed=not reasons, reasons=reasons)
    if not reasons:  # the address is released only when the contact may be contacted on this channel
        out.timezone, out.country = row["timezone"], row["country"]
        out.first_name, out.last_name = row["first_name"], row["last_name"]
        if channel == "phone":
            out.phone_e164 = row["phone_e164"]
        else:
            out.email = row["email"]
    return out


@router.post("/segments/{segment_id}/resolve", response_model=ResolveOut)
async def resolve_segment(
    segment_id: uuid.UUID, body: ResolveIn, request: Request, ictx: SegmentResolver
) -> ResolveOut:
    """Contactable members only, ordered by contact id (stable keyset)."""
    async with db(request).tenant_session(ictx.require_tenant(), ictx.actor) as s:
        segment = await load_segment(s, segment_id)
        stmt = (
            select(contacts.c.id)
            .where(await segment_clause(s, segment["rules"]), contactable_clause(body.channel))
            .order_by(contacts.c.id)
            .limit(body.limit + 1)
        )
        if body.cursor:
            stmt = stmt.where(contacts.c.id > _decode(body.cursor))
        ids = list((await s.execute(stmt)).scalars())
    more = len(ids) > body.limit
    ids = ids[: body.limit]
    return ResolveOut(
        items=[ResolvedContact(contact_id=i) for i in ids], next_cursor=_encode(ids[-1]) if more and ids else None
    )


@router.post("/contacts/{contact_id}/notes", status_code=201, response_model=NoteOut)
async def add_note(
    contact_id: uuid.UUID,
    body: NoteIn,
    request: Request,
    ictx: NoteWriter,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Any:
    tenant_id = ictx.require_tenant()
    async with db(request).tenant_session(tenant_id, ictx.actor) as s:
        op = await idem.begin(
            s,
            tenant_id=tenant_id,
            operation=f"internal.notes:{contact_id}",
            key=idem.require_key(idempotency_key),
            payload=body.model_dump(mode="json"),
        )
        if op.replay is not None:
            return op.replay
        await load_contact(s, contact_id)
        note_id = (
            await s.execute(
                insert(contact_notes)
                .values(
                    tenant_id=tenant_id,
                    contact_id=contact_id,
                    text=body.text,  # untrusted (may come from a model via a tool): stored, never interpreted
                    source_type=body.source.type,
                    source_id=body.source.id,
                    created_by=str(ictx.actor),
                )
                .returning(contact_notes.c.id)
            )
        ).scalar_one()
        return await op.done(201, {"note_id": str(note_id)}, note_id)


@router.post("/suppressions", status_code=201, response_model=SuppressionOut)
async def add(
    body: SuppressionIn,
    request: Request,
    ictx: SuppressionWriter,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Any:
    tenant_id = ictx.require_tenant()
    async with db(request).tenant_session(tenant_id, ictx.actor) as s:
        op = await idem.begin(
            s,
            tenant_id=tenant_id,
            operation="internal.suppressions",
            key=idem.require_key(idempotency_key),
            payload=body.model_dump(mode="json"),
        )
        if op.replay is not None:
            return op.replay
        suppression_id, _ = await add_suppression(
            s,
            tenant_id=tenant_id,
            channel=body.channel,
            reason=body.reason,
            source_type=body.source.type,
            source_id=body.source.id,
            actor=ictx.actor,
            contact_id=body.contact_id,
            value=body.value,
        )
        return await op.done(201, {"suppression_id": str(suppression_id)}, suppression_id)


@router.get("/suppressions/check", response_model=CheckOut)
async def check(
    request: Request,
    ictx: SuppressionChecker,
    channel: Literal["phone", "email", "all"],
    value: Annotated[str, Query(min_length=3, max_length=320)],
) -> CheckOut:
    try:
        is_email = channel == "email" or (channel == "all" and "@" in value)
        normalized = normalize_email(value) if is_email else normalize_phone(value)
    except InvalidAddress as exc:
        raise Unprocessable("Invalid value") from exc
    address_col = contacts.c.email if is_email else contacts.c.phone_e164
    channels = ("email" if is_email else "phone", "all") if channel == "all" else (channel, "all")
    async with db(request).tenant_session(ictx.require_tenant(), ictx.actor) as s:
        owner_ids = select(contacts.c.id).where(address_col == normalized)
        reasons = sorted(
            set(
                (
                    await s.execute(
                        select(suppressions.c.reason).where(
                            suppressions.c.channel.in_(channels),
                            or_(
                                suppressions.c.value_hash == value_hash(normalized),
                                suppressions.c.contact_id.in_(owner_ids),
                            ),
                        )
                    )
                ).scalars()
            )
        )
    return CheckOut(suppressed=bool(reasons), reasons=reasons)

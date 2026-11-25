"""Service-to-service API, exactly as BUILD_CONVENTIONS §5 (email-service). Caller: email-inbox-service.

`POST /internal/v1/send` (Idempotency-Key) queues one reply/transactional message. It rejects unverified
sender domains and suppressed recipients up front, and the worker checks everything again at send time.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select

from email_service.api.common import STORE, IdemHeader, db, require_key
from email_service.content import sanitize_free_html
from email_service.deps import job_context
from email_service.messages import insert_message, task_for
from email_service.policy import blocks, local_block_reason
from email_service.security import (
    address_hash,
    domain_of,
    has_crlf,
    new_rfc_message_id,
    valid_address,
    valid_message_id_list,
)
from email_service.settings import get_settings
from email_service.tables import email_messages, sender_identities, sending_domains
from nexus_core.authz import InternalContext, require_internal
from nexus_core.clients import UpstreamError, UpstreamUnavailable, service_client, tenant_settings_section
from nexus_core.errors import NotFound

router = APIRouter(prefix="/internal/v1", tags=["internal"], include_in_schema=False)
Caller = Annotated[InternalContext, Depends(require_internal(*get_settings().internal_send_clients))]
OPERATION = "internal.send"


class HeadersIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    in_reply_to: str | None = Field(default=None, alias="In-Reply-To", max_length=1000)
    references: str | None = Field(default=None, alias="References", max_length=4000)

    @field_validator("in_reply_to", "references")
    @classmethod
    def _ids(cls, v: str | None) -> str | None:
        if v is not None and not valid_message_id_list(v):
            raise ValueError("expected one or more <id@host> message ids on a single line")
        return v


class SourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    version: int | None = Field(default=None, ge=0)


class SendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["reply", "transactional"]
    from_address: str = Field(max_length=320)
    to: str = Field(max_length=320)
    subject: str = Field(min_length=1, max_length=998)
    text: str = Field(min_length=1, max_length=200_000)
    html: str | None = Field(default=None, max_length=500_000)
    headers: HeadersIn = Field(default_factory=HeadersIn)
    source: SourceIn

    @field_validator("from_address", "to")
    @classmethod
    def _address(cls, v: str) -> str:
        if not valid_address(v):
            raise ValueError("invalid email address")
        return v

    @field_validator("subject")
    @classmethod
    def _subject(cls, v: str) -> str:
        if has_crlf(v):
            raise ValueError("subject must be a single line")
        return v


class SendOut(BaseModel):
    message_id: uuid.UUID
    state: str


class LookupOut(BaseModel):
    message_id: uuid.UUID
    campaign_id: uuid.UUID | None
    contact_id: uuid.UUID | None


def _problem(status: int, code: str, detail: str) -> dict[str, Any]:
    return {"type": f"https://errors.nexuscall.dev/{code}", "title": detail, "status": status, "code": code,
            "detail": detail}  # fmt: skip


async def _finish(request: Request, tenant_id: uuid.UUID, ictx: InternalContext, record_id: uuid.UUID,
                  status: int, body: dict[str, Any], *, final: bool) -> JSONResponse:  # fmt: skip
    """Store the outcome on the idempotency record: final outcomes replay, transient ones may be retried."""
    async with db(request).tenant_session(tenant_id, ictx.actor) as s:
        if final:
            await STORE.succeed(s, record_id, status=status, body=jsonable_encoder(body))
        else:
            await STORE.fail_retryable(s, record_id)
    media = "application/json" if status < 400 else "application/problem+json"
    return JSONResponse(body, status_code=status, media_type=media)


@router.post("/send", status_code=202, response_model=SendOut)
async def send(body: SendIn, request: Request, ictx: Caller, idempotency_key: IdemHeader = None) -> Any:
    tenant_id = ictx.require_tenant()
    key = require_key(idempotency_key)
    payload = body.model_dump(mode="json", by_alias=True)
    # 1) Claim the key (committed as `processing`, so a concurrent duplicate gets 409 and a retry replays).
    async with db(request).tenant_session(tenant_id, ictx.actor) as s:
        outcome = await STORE.begin(s, tenant_id=tenant_id, operation=OPERATION, key=key, payload=payload)
    if not outcome.is_new:
        status = outcome.response_status or 202
        media = "application/json" if status < 400 else "application/problem+json"
        return JSONResponse(outcome.response_body, status_code=status, media_type=media)
    record = outcome.record_id
    # 2) Checks that need other services (no transaction held open).
    try:
        comm = await tenant_settings_section(service_client(request.app, "tenant-service"),
                                             getattr(request.app.state, "redis", None), tenant_id,
                                             "communication")  # fmt: skip
        check = await service_client(request.app, "crm-service").get_json(
            "/internal/v1/suppressions/check",
            params={"channel": "email", "value": body.to},
            tenant_id=tenant_id,
            actor=ictx.actor,
        )
    except (UpstreamError, UpstreamUnavailable):
        return await _finish(request, tenant_id, ictx, record, 503,
                             _problem(503, "upstream_unavailable", "A dependency is unavailable; retry later"),
                             final=False)  # fmt: skip
    if not comm.get("effective_emails_enabled", False):
        return await _finish(request, tenant_id, ictx, record, 409,
                             _problem(409, "emails_disabled", "Email sending is disabled for this tenant"),
                             final=False)  # fmt: skip
    if check.get("suppressed") and blocks(body.category, check.get("reasons") or ["manual"]):
        return await _finish(request, tenant_id, ictx, record, 422,
                             _problem(422, "recipient_suppressed", "The recipient is suppressed"),
                             final=True)  # fmt: skip
    # 3) Local checks + the message row + the stored response, in one transaction.
    from_domain = domain_of(body.from_address)
    message_id = uuid.uuid4()
    async with db(request).tenant_session(tenant_id, ictx.actor) as s:
        domain_status = (
            await s.execute(select(sending_domains.c.status).where(sending_domains.c.domain == from_domain))
        ).scalar_one_or_none()
        rejection: tuple[str, str] | None = None
        if domain_status != "verified":
            rejection = ("sender_domain_not_verified", "The sender domain is not verified")
        elif await local_block_reason(s, address_hash(body.to), body.category):
            rejection = ("recipient_suppressed", "The recipient is suppressed")
        if rejection is not None:
            problem = _problem(422, *rejection)
            await STORE.succeed(s, record, status=422, body=problem)
            return JSONResponse(problem, status_code=422, media_type="application/problem+json")
        sender = (
            (
                await s.execute(
                    select(sender_identities.c.id, sender_identities.c.from_name, sender_identities.c.status).where(
                        sender_identities.c.from_address == body.from_address.lower()
                    )
                )
            )
            .mappings()
            .first()
        )
        extra = {k: v for k, v in {"In-Reply-To": body.headers.in_reply_to,
                                   "References": body.headers.references}.items() if v}  # fmt: skip
        await insert_message(
            s,
            tenant_id,
            id=message_id,
            category=body.category,
            state="queued",
            sender_id=sender["id"] if sender and sender["status"] == "active" else None,
            from_address=body.from_address,
            from_name=sender["from_name"] if sender and sender["status"] == "active" else None,
            to_address=body.to,
            to_hash=address_hash(body.to),
            subject=body.subject,
            body_text=body.text,
            body_html=sanitize_free_html(body.html) if body.html else None,
            extra_headers=extra,
            rfc_message_id=new_rfc_message_id(from_domain),
            idempotency_key=str(message_id),
            source_type=body.source.type,
            source_id=body.source.id,
            source_version=body.source.version,
            queued_at=func.now(),
        )
        result = {"message_id": str(message_id), "state": "queued"}
        await STORE.succeed(s, record, status=202, body=result, resource_id=str(message_id))
    await request.app.state.enqueue(
        task_for(body.category),
        request.app.state.settings.transactional_queue,
        job_context(tenant_id, ictx.actor),
        message_id=str(message_id),
    )
    return JSONResponse(result, status_code=202)


@router.get("/messages/lookup", response_model=LookupOut)
async def lookup(
    request: Request, ictx: Caller, rfc_message_id: Annotated[str, Query(min_length=3, max_length=255)]
) -> LookupOut:
    value = rfc_message_id.strip()
    if not value.startswith("<"):
        value = f"<{value}>"
    async with db(request).tenant_session(ictx.require_tenant(), ictx.actor) as s:
        row = (
            await s.execute(
                select(email_messages.c.id, email_messages.c.campaign_id, email_messages.c.contact_id).where(
                    email_messages.c.rfc_message_id == value
                )
            )
        ).first()
    if row is None:
        raise NotFound("Message not found")
    return LookupOut(message_id=row.id, campaign_id=row.campaign_id, contact_id=row.contact_id)

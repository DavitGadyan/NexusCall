"""Sending domains and sender identities (`emails:campaigns:manage`).

Adding a domain registers it with the provider and returns the DNS records to publish (DKIM CNAMEs, SPF and
DMARC guidance). `verify` checks DNS; only `verified` domains can send.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from email_service.api.common import IdemHeader, In, Manage, Page, db, idem_begin, iso
from email_service.deps import audit
from email_service.providers.dns import dns_records
from email_service.providers.ses import to_email_credentials
from email_service.security import UnsafeValue, has_crlf, normalize_domain, valid_address
from email_service.tables import sender_identities, sending_domains
from nexus_core.clients import UpstreamError, UpstreamUnavailable
from nexus_core.errors import Conflict, NotFound, ServiceUnavailable, Unprocessable
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page

router = APIRouter(prefix="/api/v1/email", tags=["email-senders"])
_LOCAL_PART = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")


class DomainIn(In):
    domain: str = Field(min_length=3, max_length=253)


class DnsRecord(BaseModel):
    type: str
    name: str
    value: str
    purpose: str


class DomainOut(BaseModel):
    id: uuid.UUID
    domain: str
    status: str
    dkim_status: str
    spf_status: str
    dmarc_status: str
    records: list[DnsRecord]
    last_checked_at: str | None
    verified_at: str | None
    created_at: str
    version: int


class DomainList(Page):
    items: list[DomainOut]


class SenderIn(In):
    domain_id: uuid.UUID
    from_name: str = Field(min_length=1, max_length=120)
    local_part: str = Field(min_length=1, max_length=64)
    reply_to: str | None = Field(default=None, max_length=320)

    @field_validator("from_name")
    @classmethod
    def _name(cls, v: str) -> str:
        if has_crlf(v) or any(c in v for c in '<>"'):
            raise ValueError("display name must be one line without <, > or quotes")
        return v

    @field_validator("local_part")
    @classmethod
    def _local(cls, v: str) -> str:
        if not _LOCAL_PART.match(v):
            raise ValueError("invalid local part")
        return v

    @field_validator("reply_to")
    @classmethod
    def _reply(cls, v: str | None) -> str | None:
        if v is not None and not valid_address(v):
            raise ValueError("invalid reply-to address")
        return v


class SenderPatch(In):
    expected_version: int = Field(ge=1)
    from_name: str | None = Field(default=None, min_length=1, max_length=120)
    reply_to: str | None = Field(default=None, max_length=320)
    status: str | None = Field(default=None, pattern=r"^(active|disabled)$")

    @field_validator("from_name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        return None if v is None else SenderIn._name(v)

    @field_validator("reply_to")
    @classmethod
    def _reply(cls, v: str | None) -> str | None:
        return SenderIn._reply(v)


class SenderOut(BaseModel):
    id: uuid.UUID
    domain_id: uuid.UUID
    domain: str
    domain_status: str
    from_name: str
    from_address: str
    reply_to: str | None
    status: str
    created_at: str
    version: int


class SenderList(Page):
    items: list[SenderOut]


def _domain_out(row: Any) -> DomainOut:
    return DomainOut(
        id=row["id"],
        domain=row["domain"],
        status=row["status"],
        dkim_status=row["dkim_status"],
        spf_status=row["spf_status"],
        dmarc_status=row["dmarc_status"],
        records=[DnsRecord(**r) for r in dns_records(row["domain"], list(row["dkim_tokens"]))],
        last_checked_at=iso(row["last_checked_at"]),
        verified_at=iso(row["verified_at"]),
        created_at=row["created_at"].isoformat(),
        version=row["version"],
    )


async def _load_domain(session: Any, domain_id: uuid.UUID) -> Any:
    row = (await session.execute(select(sending_domains).where(sending_domains.c.id == domain_id))).mappings().first()
    if row is None:
        raise NotFound("Domain not found")
    return row


@router.get("/domains", response_model=DomainList)
async def list_domains(request: Request, ctx: Manage, limit: Limit = 50, cursor: Cursor = None) -> DomainList:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        stmt = apply_keyset(select(sending_domains), sending_domains.c.created_at, sending_domains.c.id, cursor, limit)
        rows = [dict(r) for r in (await s.execute(stmt)).mappings().all()]
    page, nxt = build_page(rows, limit)
    return DomainList(items=[_domain_out(r) for r in page], next_cursor=nxt)


@router.post("/domains", status_code=201, response_model=DomainOut)
async def add_domain(body: DomainIn, request: Request, ctx: Manage, idempotency_key: IdemHeader = None) -> Any:
    tenant_id = ctx.require_tenant()
    try:
        domain = normalize_domain(body.domain)
    except UnsafeValue as exc:
        raise Unprocessable("Invalid domain name") from exc
    # Register the identity with the provider first (one bounded call): it issues the DKIM tokens.
    try:
        resolved = await request.app.state.credentials.resolve(
            tenant_id, "ses", actor=ctx.actor, allow_platform_default=True
        )
        tokens = await request.app.state.provider.create_domain_identity(to_email_credentials(resolved), domain)
    except (UpstreamError, UpstreamUnavailable) as exc:
        raise ServiceUnavailable("Email provider connection could not be resolved") from exc
    except Exception as exc:
        if getattr(exc, "status", None) == 409:
            raise
        raise ServiceUnavailable("Email provider rejected or did not answer the identity request") from exc
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        op = await idem_begin(s, tenant_id=tenant_id, operation="domains.create", key=idempotency_key,
                              payload=body.model_dump())  # fmt: skip
        if op.replay is not None:
            return op.replay
        row = (
            (
                await s.execute(
                    insert(sending_domains)
                    .values(tenant_id=tenant_id, domain=domain, dkim_tokens=tokens, created_by=str(ctx.actor))
                    .on_conflict_do_nothing(constraint="uq_sending_domains_domain")
                    .returning(sending_domains)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise Conflict("This domain is already registered")
        await audit(s, ctx.actor, "email_domain.created", "email_domain", row["id"], tenant_id=tenant_id)
        return await op.done(201, _domain_out(row).model_dump(mode="json"), row["id"])


@router.get("/domains/{domain_id}", response_model=DomainOut)
async def get_domain(domain_id: uuid.UUID, request: Request, ctx: Manage) -> DomainOut:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        return _domain_out(await _load_domain(s, domain_id))


@router.post("/domains/{domain_id}/verify", response_model=DomainOut)
async def verify_domain(domain_id: uuid.UUID, request: Request, ctx: Manage) -> DomainOut:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        row = await _load_domain(s, domain_id)
    if row["status"] == "disabled":
        raise Conflict("Domain is disabled")
    result = await request.app.state.dns.check(row["domain"], list(row["dkim_tokens"]))
    status = "verified" if result.verified else "failed"
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        updated = (
            (
                await s.execute(
                    update(sending_domains)
                    .where(sending_domains.c.id == domain_id, sending_domains.c.status != "disabled")
                    .values(
                        status=status,
                        dkim_status=result.dkim,
                        spf_status=result.spf,
                        dmarc_status=result.dmarc,
                        last_checked_at=func.now(),
                        verified_at=func.now() if result.verified else None,
                        version=sending_domains.c.version + 1,
                        updated_at=func.now(),
                    )
                    .returning(sending_domains)
                )
            )
            .mappings()
            .first()
        )
        if updated is None:
            raise Conflict("Domain is disabled")
        await audit(
            s, ctx.actor, "email_domain.verification_checked", "email_domain", domain_id, tenant_id=tenant_id,
            status=status, dkim=result.dkim, spf=result.spf, dmarc=result.dmarc,
        )  # fmt: skip
    return _domain_out(updated)


@router.delete("/domains/{domain_id}", status_code=204)
async def delete_domain(domain_id: uuid.UUID, request: Request, ctx: Manage) -> Response:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        await _load_domain(s, domain_id)
        used = (await s.execute(select(func.count()).where(sender_identities.c.domain_id == domain_id))).scalar_one()
        if used:
            raise Conflict("Remove or disable the sender identities on this domain first")
        await s.execute(sending_domains.delete().where(sending_domains.c.id == domain_id))
        await audit(s, ctx.actor, "email_domain.deleted", "email_domain", domain_id, tenant_id=tenant_id)
    return Response(status_code=204)


# ------------------------------------------------------------------------------------------------- senders
_SENDER_COLS = (
    sender_identities,
    sending_domains.c.domain.label("domain"),
    sending_domains.c.status.label("domain_status"),
)


def _sender_select() -> Any:
    return select(*_SENDER_COLS).select_from(
        sender_identities.join(
            sending_domains,
            (sending_domains.c.id == sender_identities.c.domain_id)
            & (sending_domains.c.tenant_id == sender_identities.c.tenant_id),
        )
    )


def _sender_out(row: Any) -> SenderOut:
    return SenderOut(
        id=row["id"],
        domain_id=row["domain_id"],
        domain=row["domain"],
        domain_status=row["domain_status"],
        from_name=row["from_name"],
        from_address=row["from_address"],
        reply_to=row["reply_to"],
        status=row["status"],
        created_at=row["created_at"].isoformat(),
        version=row["version"],
    )


async def load_sender(session: Any, sender_id: uuid.UUID) -> Any:
    row = (await session.execute(_sender_select().where(sender_identities.c.id == sender_id))).mappings().first()
    if row is None:
        raise NotFound("Sender identity not found")
    return row


@router.get("/senders", response_model=SenderList)
async def list_senders(request: Request, ctx: Manage, limit: Limit = 50, cursor: Cursor = None) -> SenderList:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        stmt = apply_keyset(_sender_select(), sender_identities.c.created_at, sender_identities.c.id, cursor, limit)
        rows = [dict(r) for r in (await s.execute(stmt)).mappings().all()]
    page, nxt = build_page(rows, limit)
    return SenderList(items=[_sender_out(r) for r in page], next_cursor=nxt)


@router.post("/senders", status_code=201, response_model=SenderOut)
async def add_sender(body: SenderIn, request: Request, ctx: Manage, idempotency_key: IdemHeader = None) -> Any:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        op = await idem_begin(s, tenant_id=tenant_id, operation="senders.create", key=idempotency_key,
                              payload=body.model_dump(mode="json"))  # fmt: skip
        if op.replay is not None:
            return op.replay
        domain = await _load_domain(s, body.domain_id)
        address = f"{body.local_part}@{domain['domain']}"
        if not valid_address(address):
            raise Unprocessable("Invalid sender address")
        sender_id = (
            await s.execute(
                insert(sender_identities)
                .values(
                    tenant_id=tenant_id,
                    domain_id=body.domain_id,
                    from_name=body.from_name,
                    from_address=address.lower(),
                    reply_to=body.reply_to,
                    created_by=str(ctx.actor),
                )
                .on_conflict_do_nothing(constraint="uq_sender_identities_address")
                .returning(sender_identities.c.id)
            )
        ).scalar_one_or_none()
        if sender_id is None:
            raise Conflict("This sender address already exists")
        await audit(s, ctx.actor, "email_sender.created", "email_sender", sender_id, tenant_id=tenant_id)
        out = _sender_out(await load_sender(s, sender_id))
        return await op.done(201, out.model_dump(mode="json"), sender_id)


@router.get("/senders/{sender_id}", response_model=SenderOut)
async def get_sender(sender_id: uuid.UUID, request: Request, ctx: Manage) -> SenderOut:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        return _sender_out(await load_sender(s, sender_id))


@router.patch("/senders/{sender_id}", response_model=SenderOut)
async def patch_sender(sender_id: uuid.UUID, body: SenderPatch, request: Request, ctx: Manage) -> SenderOut:
    tenant_id = ctx.require_tenant()
    changes = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        await load_sender(s, sender_id)
        done = await s.execute(
            update(sender_identities)
            .where(sender_identities.c.id == sender_id, sender_identities.c.version == body.expected_version)
            .values(**changes, version=sender_identities.c.version + 1, updated_at=func.now())
        )
        if done.rowcount != 1:  # type: ignore[attr-defined]
            raise Conflict("Sender identity was changed by someone else; reload and retry")
        await audit(s, ctx.actor, "email_sender.updated", "email_sender", sender_id, tenant_id=tenant_id,
                    fields=sorted(changes))  # fmt: skip
        return _sender_out(await load_sender(s, sender_id))

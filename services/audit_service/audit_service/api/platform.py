"""Platform audit (Platform Admin Console, build.md §18.1A "Audit and compliance", §20.3).

Cross-tenant reads use the dedicated read-only `audit_platform` database role (its own SELECT policy),
never the runtime role and never an RLS bypass. Every search and every verification is itself recorded:
who, which filters, how many results.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from audit_service.api.common import (
    LIST_COLUMNS,
    AuditFilters,
    PlatformAuditEventOut,
    PlatformAuditPage,
    audit_filters,
    db,
    platform_db,
    record_self,
)
from audit_service.chain import HASHED_FIELDS, ChainVerifier, chain_for
from audit_service.tables import audit_events
from nexus_core.authz import AuthContext, require_permission
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page

router = APIRouter(prefix="/api/v1/platform/audit", tags=["platform-audit"])
PlatformAudit = Annotated[AuthContext, Depends(require_permission("platform:audit:read"))]
Filters = Annotated[AuditFilters, Depends(audit_filters)]


@router.get("/events", response_model=PlatformAuditPage)
async def search_events(
    request: Request,
    ctx: PlatformAudit,
    filters: Filters,
    tenant_id: uuid.UUID | None = None,
    platform_only: bool = Query(False, description="Only platform-level events (no tenant)"),
    limit: Limit = 50,
    cursor: Cursor = None,
) -> dict[str, Any]:
    stmt = filters.apply(select(*LIST_COLUMNS))
    if platform_only:
        stmt = stmt.where(audit_events.c.tenant_id.is_(None))
    elif tenant_id is not None:
        stmt = stmt.where(audit_events.c.tenant_id == tenant_id)
    stmt = apply_keyset(stmt, audit_events.c.occurred_at, audit_events.c.id, cursor, limit)
    async with platform_db(request).global_session(ctx.actor) as s:
        rows = (await s.execute(stmt)).mappings().all()
    page, next_cursor = build_page(list(rows), limit, created_attr="occurred_at")
    # The search itself is audited. A search scoped to one tenant lands in that tenant's chain, so the
    # tenant's own audit log shows that the platform looked at it.
    await record_self(
        db(request),
        actor=ctx.actor,
        tenant_id=None if platform_only else tenant_id,
        action="platform.audit.searched",
        resource_type="audit_events",
        metadata={
            **filters.describe(),
            "tenant_id": str(tenant_id) if tenant_id else None,
            "platform_only": platform_only,
            "paged": cursor is not None,
            "result_count": len(page),
        },
    )
    return {
        "items": [{k: r[k] for k in PlatformAuditEventOut.model_fields} for r in page],
        "next_cursor": next_cursor,
    }


class VerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: uuid.UUID | None = None  # None verifies the platform chain


class BrokenLinkOut(BaseModel):
    chain_seq: int
    id: str
    reason: str


class VerifyOut(BaseModel):
    chain: str
    ok: bool
    checked: int
    head_seq: int
    first_broken: BrokenLinkOut | None


def _verify_columns() -> list[Any]:
    names = ("id", "prev_hash", "hash", *HASHED_FIELDS)
    return [audit_events.c[n] for n in dict.fromkeys(names)]


@router.post("/verify", response_model=VerifyOut)
async def verify_chain(body: VerifyIn, request: Request, ctx: PlatformAudit) -> dict[str, Any]:
    """Recompute a chain from genesis and report the first broken link (sequence gap, wrong prev_hash,
    or a row whose content no longer matches its hash)."""
    chain = chain_for(body.tenant_id)
    batch = int(request.app.state.settings.verify_batch_size)
    verifier = ChainVerifier()
    columns = _verify_columns()
    last_seq = 0
    async with platform_db(request).global_session(ctx.actor) as s:
        while True:
            rows = (
                (
                    await s.execute(
                        select(*columns)
                        .where(audit_events.c.chain == chain, audit_events.c.chain_seq > last_seq)
                        .order_by(audit_events.c.chain_seq)
                        .limit(batch)
                    )
                )
                .mappings()
                .all()
            )
            if not rows or not verifier.feed(rows):
                break
            last_seq = int(rows[-1]["chain_seq"])
    broken = verifier.broken
    await record_self(
        db(request),
        actor=ctx.actor,
        tenant_id=None,
        action="platform.audit.chain_verified",
        resource_type="audit_chain",
        resource_id=chain,
        outcome="success" if broken is None else "failure",
        metadata={
            "checked": verifier.checked,
            "first_broken_seq": broken.chain_seq if broken else None,
            "reason": broken.reason if broken else None,
        },
    )
    return {
        "chain": chain,
        "ok": broken is None,
        "checked": verifier.checked,
        "head_seq": verifier.expected_seq - 1,
        "first_broken": (
            {"chain_seq": broken.chain_seq, "id": broken.row_id, "reason": broken.reason} if broken else None
        ),
    }

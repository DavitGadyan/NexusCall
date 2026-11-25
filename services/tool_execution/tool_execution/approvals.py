"""Approval state machine (pending → approved → consumed | rejected | expired). Persisted server-side state is
the only thing that can authorize a Class 3 action (build.md §24.11 item 11)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, update

from nexus_core.context import Actor
from nexus_core.db import Database
from tool_execution.events import audit
from tool_execution.tables import ai_approval_requests, approval_tenants

TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"approved", "rejected", "expired"}),
    "approved": frozenset({"consumed", "expired"}),
    "rejected": frozenset(),
    "expired": frozenset(),
    "consumed": frozenset(),
}

EXPIRY_ACTOR = Actor("system", "approval-expiry")


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def effective_status(row: dict[str, Any], now: Any) -> str:
    if row["status"] in ("pending", "approved") and row["expires_at"] <= now:
        return "expired"
    return str(row["status"])


async def claim(db: Database, tenant_id: uuid.UUID, approval_id: uuid.UUID, actor: Actor) -> dict[str, Any] | None:
    """Atomically consume an approved, unexpired approval. Only one caller can ever win."""
    async with db.tenant_session(tenant_id, actor) as s:
        row = (
            (
                await s.execute(
                    update(ai_approval_requests)
                    .where(
                        ai_approval_requests.c.id == approval_id,
                        ai_approval_requests.c.status == "approved",
                        ai_approval_requests.c.expires_at > func.now(),
                    )
                    .values(
                        status="consumed",
                        consumed_at=func.now(),
                        updated_at=func.now(),
                        version=ai_approval_requests.c.version + 1,
                    )
                    .returning(*ai_approval_requests.c)
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None


async def expire_approvals(db: Database) -> int:
    """Periodic job: expire pending/approved requests past their deadline (idempotent, per-tenant RLS)."""
    async with db.global_session(EXPIRY_ACTOR) as s:
        tenants = (await s.execute(select(approval_tenants.c.tenant_id))).scalars().all()
    total = 0
    for tenant_id in tenants:
        async with db.tenant_session(tenant_id, EXPIRY_ACTOR) as s:
            rows = (
                await s.execute(
                    update(ai_approval_requests)
                    .where(
                        ai_approval_requests.c.status.in_(("pending", "approved")),
                        ai_approval_requests.c.expires_at <= func.now(),
                    )
                    .values(status="expired", updated_at=func.now(), version=ai_approval_requests.c.version + 1)
                    .returning(ai_approval_requests.c.id, ai_approval_requests.c.tool_id)
                )
            ).all()
            for approval_id, tool_id in rows:
                total += 1
                await audit(
                    s, EXPIRY_ACTOR, "ai.tool.approval_expired", "ai_approval_request", approval_id,
                    tenant_id=tenant_id, tool_id=tool_id,
                )  # fmt: skip
    return total

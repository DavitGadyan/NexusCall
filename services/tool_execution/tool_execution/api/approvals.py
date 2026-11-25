"""Human approval of AI-proposed Class 3 actions (`ai:approvals:decide`), and the tool execution trail.

Approve requires step-up MFA, re-checks the permission at that moment, accepts only human principals, and
executes exactly once from the STORED canonical arguments (never anything the model says later)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update

from nexus_core.authz import AuthContext, require_permission, require_step_up
from nexus_core.errors import Conflict, Forbidden, NotFound
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page
from tool_execution.approvals import claim, effective_status
from tool_execution.events import audit
from tool_execution.executor import Executor
from tool_execution.tables import ai_approval_requests, tool_execution_records

router = APIRouter(prefix="/api/v1/ai", tags=["ai-tool-approvals"])
Decider = Annotated[AuthContext, Depends(require_permission("ai:approvals:decide"))]
UsageReader = Annotated[AuthContext, Depends(require_permission("ai:usage:read"))]
StepUp = Annotated[AuthContext, Depends(require_step_up())]


class Requester(BaseModel):
    type: str
    id: str


class ApprovalOut(BaseModel):
    id: uuid.UUID
    tool_id: str
    tool_version: int
    risk_class: int
    agent_id: uuid.UUID
    arguments: dict[str, Any]
    context: dict[str, Any]
    status: str
    requested_by: Requester
    created_at: datetime
    expires_at: datetime
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    decision_note: str | None
    consumed_at: datetime | None
    result_status: str | None
    result_reason: str | None


class ApprovalPage(BaseModel):
    items: list[ApprovalOut]
    next_cursor: str | None


class ExecutionOut(BaseModel):
    status: str
    result: Any = None
    reason: str | None = None


class DecisionOut(BaseModel):
    approval: ApprovalOut
    execution: ExecutionOut | None = None


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field("", max_length=500)


def _out(row: Any) -> ApprovalOut:
    r = dict(row)
    return ApprovalOut(
        **{k: r[k] for k in ApprovalOut.model_fields if k in r and k not in ("status", "requested_by")},
        status=effective_status(r, datetime.now(UTC)),
        requested_by=Requester(type=r["requested_by_type"], id=r["requested_by_id"]),
    )


async def _load(request: Request, ctx: AuthContext, approval_id: uuid.UUID) -> dict[str, Any]:
    tenant_id = ctx.require_tenant()
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as s:
        row = (
            (await s.execute(select(ai_approval_requests).where(ai_approval_requests.c.id == approval_id)))
            .mappings()
            .first()
        )
    if row is None:
        raise NotFound("Approval request not found")
    return dict(row)


@router.get("/tool-approvals", response_model=ApprovalPage)
async def list_approvals(
    request: Request,
    ctx: Decider,
    status: Literal["pending", "approved", "rejected", "expired", "consumed"] | None = Query(default=None),
    limit: Limit = 50,
    cursor: Cursor = None,
) -> Any:
    tenant_id = ctx.require_tenant()
    t = ai_approval_requests
    stmt = select(t)
    if status == "pending":
        stmt = stmt.where(t.c.status == "pending", t.c.expires_at > func.now())
    elif status == "expired":
        stmt = stmt.where(
            (t.c.status == "expired") | (t.c.status.in_(("pending", "approved")) & (t.c.expires_at <= func.now()))
        )
    elif status is not None:
        stmt = stmt.where(t.c.status == status)
    stmt = apply_keyset(stmt, t.c.created_at, t.c.id, cursor, limit)
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as s:
        rows = (await s.execute(stmt)).mappings().all()
    page, nxt = build_page(list(rows), limit)
    return ApprovalPage(items=[_out(r) for r in page], next_cursor=nxt)


@router.get("/tool-approvals/{approval_id}", response_model=ApprovalOut)
async def get_approval(approval_id: uuid.UUID, request: Request, ctx: Decider) -> Any:
    return _out(await _load(request, ctx, approval_id))


def _human(ctx: AuthContext) -> uuid.UUID:
    if ctx.principal.is_service:
        raise Forbidden("Only a person can decide on an AI action")
    try:
        return uuid.UUID(ctx.principal.sub)
    except ValueError as exc:
        raise Forbidden("Only a person can decide on an AI action") from exc


async def _decide(
    request: Request, ctx: AuthContext, approval_id: uuid.UUID, decision: str, note: str
) -> dict[str, Any]:
    decider = _human(ctx)
    tenant_id = ctx.require_tenant()
    t = ai_approval_requests
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as s:
        row = (await s.execute(select(t).where(t.c.id == approval_id).with_for_update())).mappings().first()
        if row is None:
            raise NotFound("Approval request not found")
        if row["status"] != "pending":
            raise Conflict(f"This request is already {row['status']}")
        if row["expires_at"] <= datetime.now(UTC):
            await s.execute(update(t).where(t.c.id == approval_id).values(status="expired", updated_at=func.now()))
            await audit(
                s, ctx.actor, "ai.tool.approval_expired", "ai_approval_request", approval_id, tenant_id=tenant_id
            )
            expired = True
        else:
            expired = False
            if row["requested_by_type"] == "user" and row["requested_by_id"] == str(decider):
                raise Forbidden("You cannot approve an action you requested yourself")
            await s.execute(
                update(t)
                .where(t.c.id == approval_id, t.c.status == "pending")
                .values(
                    status=decision,
                    decided_by=decider,
                    decided_at=func.now(),
                    decision_note=note or None,
                    updated_at=func.now(),
                    version=t.c.version + 1,
                )
            )
            await audit(
                s, ctx.actor, f"ai.tool.{decision}", "ai_approval_request", approval_id, tenant_id=tenant_id,
                tool_id=row["tool_id"], agent_id=str(row["agent_id"]), risk_class=row["risk_class"],
            )  # fmt: skip
    if expired:  # committed above; reported after the transaction so the expiry sticks
        raise Conflict("This request has expired")
    return row  # type: ignore[return-value]


@router.post("/tool-approvals/{approval_id}/approve", response_model=DecisionOut)
async def approve(approval_id: uuid.UUID, body: DecisionIn, request: Request, ctx: Decider, _: StepUp) -> Any:
    await _decide(request, ctx, approval_id, "approved", body.note)
    tenant_id = ctx.require_tenant()
    claimed = await claim(request.app.state.db, tenant_id, approval_id, ctx.actor)
    execution = None
    if claimed is not None:  # exactly one approver request ever gets here for a given approval
        executor: Executor = request.app.state.executor
        outcome = await executor.run_approved(tenant_id, claimed, ctx.actor)
        execution = ExecutionOut(status=outcome.status, result=outcome.result, reason=outcome.reason)
    return DecisionOut(approval=_out(await _load(request, ctx, approval_id)), execution=execution)


@router.post("/tool-approvals/{approval_id}/reject", response_model=DecisionOut)
async def reject(approval_id: uuid.UUID, body: DecisionIn, request: Request, ctx: Decider) -> Any:
    await _decide(request, ctx, approval_id, "rejected", body.note)
    return DecisionOut(approval=_out(await _load(request, ctx, approval_id)))


# ---------------------------------------------------------------- execution trail (metadata only)
class ExecutionRecordOut(BaseModel):
    id: uuid.UUID
    tool_id: str
    tool_version: int | None
    risk_class: int | None
    target_kind: str | None
    mcp_server_id: uuid.UUID | None
    agent_id: uuid.UUID
    actor_type: str
    actor_id: str
    call_id: uuid.UUID | None
    contact_id: uuid.UUID | None
    thread_id: uuid.UUID | None
    status: str
    reason: str | None
    approval_id: uuid.UUID | None
    policy_version: int | None
    duration_ms: int
    output_bytes: int
    correlation_id: str | None
    created_at: datetime


class ExecutionPage(BaseModel):
    items: list[ExecutionRecordOut]
    next_cursor: str | None


@router.get("/tool-executions", response_model=ExecutionPage)
async def list_executions(
    request: Request,
    ctx: UsageReader,
    agent_id: uuid.UUID | None = None,
    status: Literal["executed", "approval_required", "denied", "failed"] | None = None,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> Any:
    tenant_id = ctx.require_tenant()
    t = tool_execution_records
    stmt = select(t)
    if agent_id is not None:
        stmt = stmt.where(t.c.agent_id == agent_id)
    if status is not None:
        stmt = stmt.where(t.c.status == status)
    stmt = apply_keyset(stmt, t.c.created_at, t.c.id, cursor, limit)
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as s:
        rows = (await s.execute(stmt)).mappings().all()
    page, nxt = build_page(list(rows), limit)
    return ExecutionPage(items=[ExecutionRecordOut.model_validate(dict(r)) for r in page], next_cursor=nxt)

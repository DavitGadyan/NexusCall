"""Public call API (calls:read, calls:manage). Every read runs inside the caller's tenant session (RLS)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from call_service.api.internal import cancel_or_hangup, check_contactable, insert_outbound_call
from call_service.deps import AppDeps
from call_service.domain import after_commit, audit, job_ctx, load_call
from call_service.schemas import CallOut, CallPage, CallTestIn, CreateCallOut, HangupOut, call_out
from call_service.state_machine import LIVE
from call_service.tables import call_outcomes, calls, idempotency_keys
from nexus_core.authz import AuthContext, require_permission
from nexus_core.errors import Unprocessable
from nexus_core.idempotency import IdempotencyStore
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page

router = APIRouter(prefix="/api/v1/calls", tags=["calls"])
Read = Annotated[AuthContext, Depends(require_permission("calls:read"))]
Manage = Annotated[AuthContext, Depends(require_permission("calls:manage"))]
idem = IdempotencyStore(idempotency_keys)
StateFilter = Literal[
    "queued", "scheduled", "initiating", "ringing", "connected", "in_progress", "completed", "failed", "canceled",
    "opted_out",
]  # fmt: skip


async def _outcomes(s: Any, ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    if not ids:
        return {}
    rows = (
        (
            await s.execute(
                select(call_outcomes.c.call_id, call_outcomes.c.outcome, call_outcomes.c.qualified).where(
                    call_outcomes.c.call_id.in_(ids)
                )
            )
        )
        .mappings()
        .all()
    )
    return {r["call_id"]: dict(r) for r in rows}


@router.get("", response_model=CallPage)
async def list_calls(
    request: Request,
    ctx: Read,
    limit: Limit = 50,
    cursor: Cursor = None,
    state: StateFilter | None = None,
    direction: Literal["inbound", "outbound"] | None = None,
    campaign_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
) -> CallPage:
    stmt = select(calls)
    if state:
        stmt = stmt.where(calls.c.state == state)
    if direction:
        stmt = stmt.where(calls.c.direction == direction)
    if campaign_id:
        stmt = stmt.where(calls.c.campaign_id == campaign_id)
    if contact_id:
        stmt = stmt.where(calls.c.contact_id == contact_id)
    async with AppDeps(request.app).db.tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = [
            dict(r)
            for r in (await s.execute(apply_keyset(stmt, calls.c.created_at, calls.c.id, cursor, limit))).mappings()
        ]
        page, next_cursor = build_page(rows, limit)
        outcomes = await _outcomes(s, [r["id"] for r in page])
    return CallPage(items=[call_out(r, outcomes.get(r["id"])) for r in page], next_cursor=next_cursor)


@router.get("/live", response_model=CallPage)
async def live_calls(request: Request, ctx: Read) -> CallPage:
    async with AppDeps(request.app).db.tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = (
            (
                await s.execute(
                    select(calls).where(calls.c.state.in_(sorted(LIVE))).order_by(calls.c.created_at.desc()).limit(200)
                )
            )
            .mappings()
            .all()
        )
    return CallPage(items=[call_out(dict(r)) for r in rows], next_cursor=None)


@router.get("/{call_id}", response_model=CallOut)
async def get_call(call_id: uuid.UUID, request: Request, ctx: Read) -> CallOut:
    async with AppDeps(request.app).db.tenant_session(ctx.require_tenant(), ctx.actor) as s:
        call = await load_call(s, call_id)
        outcomes = await _outcomes(s, [call_id])
    return call_out(call, outcomes.get(call_id))


@router.post("/{call_id}/hangup", response_model=HangupOut)
async def hangup(call_id: uuid.UUID, request: Request, ctx: Manage) -> HangupOut:
    """Supervisor hangup. The permission is checked now (not cached in the UI) and the action is audited."""
    deps = AppDeps(request.app)
    tenant_id = ctx.require_tenant()
    row, action = await cancel_or_hangup(deps, tenant_id, call_id, "supervisor_hangup", ctx.actor)
    async with deps.db.tenant_session(tenant_id, ctx.actor) as s:
        await audit(s, ctx.actor, "call.hangup", "call", call_id, tenant_id=tenant_id, result=action)
    return HangupOut(call_id=call_id, state=row["state"], hangup=action)  # type: ignore[arg-type]


@router.post("/test", status_code=201, response_model=CreateCallOut)
async def test_call(
    body: CallTestIn,
    request: Request,
    ctx: Manage,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Any:
    """Place a test call to an existing, contactable contact. All pre-dial gates apply (no free-form numbers)."""
    if not idempotency_key:
        raise Unprocessable("Idempotency-Key header is required")
    deps = AppDeps(request.app)
    tenant_id = ctx.require_tenant()
    async with deps.db.tenant_session(tenant_id, ctx.actor) as s:
        op = await idem.begin(
            s,
            tenant_id=tenant_id,
            operation="api.calls.test",
            key=idempotency_key,
            payload=body.model_dump(mode="json"),
        )
        if not op.is_new:
            return JSONResponse(op.response_body, status_code=op.response_status or 201)
        await check_contactable(deps, tenant_id, body.contact_id, ctx.actor)
        call = await insert_outbound_call(
            s,
            deps,
            tenant_id=tenant_id,
            actor=ctx.actor,
            contact_id=body.contact_id,
            agent_id=body.agent_id,
            is_test=True,
        )
        await audit(s, ctx.actor, "call.test.created", "call", call["id"], tenant_id=tenant_id)
        out = {"call_id": str(call["id"]), "state": call["state"]}
        await idem.succeed(s, op.record_id, status=201, body=out, resource_id=str(call["id"]))
    await after_commit(deps.redis, call)
    await deps.enqueue("calls.dispatch", "calls.dispatch", job_ctx(tenant_id, ctx.actor), call_id=str(call["id"]))
    return JSONResponse(out, status_code=201)

"""`POST /internal/v1/tools/execute` (BUILD_CONVENTIONS §5). The only caller is the AI gateway.

Identity, tenant and resource scope come from the verified service token, the X-Tenant-ID / X-Actor headers
and the bound `context`; never from `arguments`. The body is strict: an `approval_id`, `approved` flag or
any other extra field is rejected before anything runs.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_core.authz import InternalContext, require_internal
from nexus_core.errors import Unprocessable
from nexus_core.idempotency import IdempotencyStore
from tool_execution.executor import Executor, Outcome, ToolCall
from tool_execution.settings import get_settings
from tool_execution.tables import idempotency_keys

router = APIRouter(prefix="/internal/v1", tags=["internal"], include_in_schema=False)
_gateway = require_internal(*get_settings().gateway_clients)
_idempotency = IdempotencyStore(idempotency_keys)


class ContextIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    call_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    thread_id: uuid.UUID | None = None


class ExecuteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_id: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any]
    agent_id: uuid.UUID
    context: ContextIn = Field(default_factory=ContextIn)
    idempotency_key: str = Field(min_length=1, max_length=255)


class ExecuteOut(BaseModel):
    status: Literal["executed", "approval_required", "denied", "failed"]
    result: Any = None
    approval_id: uuid.UUID | None = None
    reason: str | None = None


@router.post("/tools/execute", response_model=ExecuteOut, response_model_exclude_none=True)
async def execute(
    body: ExecuteIn,
    request: Request,
    ictx: InternalContext = Depends(_gateway),
    idempotency_key: str | None = Header(default=None, max_length=255),
) -> Any:
    tenant_id = ictx.require_tenant()
    if idempotency_key is not None and idempotency_key != body.idempotency_key:
        raise Unprocessable("Idempotency-Key header and body idempotency_key differ")
    async with request.app.state.db.tenant_session(tenant_id, ictx.actor) as s:
        op = await _idempotency.begin(
            s,
            tenant_id=tenant_id,
            operation="tools.execute",
            key=body.idempotency_key,
            payload=body.model_dump(mode="json"),
        )
    if not op.is_new:
        return op.response_body

    async def finalize(session: AsyncSession, outcome: Outcome) -> None:
        if outcome.transient:
            await _idempotency.fail_retryable(session, op.record_id)
        else:
            await _idempotency.succeed(session, op.record_id, status=200, body=outcome.body())

    executor: Executor = request.app.state.executor
    call = ToolCall(
        tenant_id=tenant_id,
        actor=ictx.actor,
        caller=ictx.principal.azp or "unknown",
        agent_id=body.agent_id,
        tool_id=body.tool_id,
        arguments=body.arguments,
        context={k: str(v) for k, v in body.context.model_dump().items() if v is not None},
        idempotency_key=body.idempotency_key,
    )
    outcome = await executor.execute(call, finalize=finalize)
    return outcome.body()

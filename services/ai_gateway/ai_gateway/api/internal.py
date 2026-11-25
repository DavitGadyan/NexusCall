"""Internal model access API. Only allow-listed caller services; tenant and actor from verified headers."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ai_gateway.orchestrator import Gateway, RunState
from ai_gateway.policy import PolicyDenied
from ai_gateway.schemas import EmbeddingsOut, EmbeddingsRequest, ResponseOut, ResponseRequest, Usage
from ai_gateway.settings import PURPOSE_DEADLINE_SECONDS, get_settings
from nexus_core.authz import InternalContext, require_internal

router = APIRouter(prefix="/internal/v1/ai", tags=["internal"])
Caller = Annotated[InternalContext, Depends(require_internal(*get_settings().internal_client_allowlist))]


def _gateway(request: Request) -> Gateway:
    return request.app.state.gateway  # type: ignore[no-any-return]


@router.post("/responses", response_model=ResponseOut, response_model_by_alias=True)
async def responses(body: ResponseRequest, request: Request, ctx: Caller) -> ResponseOut:
    gw = _gateway(request)
    state = RunState(request_id=uuid.uuid4())
    tenant_id = ctx.require_tenant()
    prep = None
    try:
        async with gw.slot(tenant_id, state.request_id, int(PURPOSE_DEADLINE_SECONDS[body.purpose]) + 30):
            prep = await gw.prepare(body, ctx, state)
            text, parsed, stop_reason = await gw.run(prep, state)
    except PolicyDenied as exc:
        await gw.persist(
            req=body,
            ctx=ctx,
            state=state,
            status="denied",
            deny_reason=exc.reason,
            stop_reason=None,
            prep=prep,
            purpose=body.purpose,
        )
        raise
    except Exception:
        await gw.persist(
            req=body,
            ctx=ctx,
            state=state,
            status="failed",
            deny_reason=None,
            stop_reason=None,
            prep=prep,
            purpose=body.purpose,
        )
        raise
    status = "blocked_output" if "output_blocked" in state.flags else "completed"
    await gw.persist(
        req=body,
        ctx=ctx,
        state=state,
        status=status,
        deny_reason=None,
        stop_reason=stop_reason,
        prep=prep,
        purpose=body.purpose,
    )
    assert state.model is not None
    return ResponseOut(
        request_id=state.request_id,
        text=text,
        json=parsed,
        stop_reason=stop_reason,
        tool_calls=state.tool_calls,
        usage=Usage(input_tokens=state.input_tokens, output_tokens=state.output_tokens, cost_micros=state.cost),
        model=state.model.model_id,
        provider=state.model.provider,
        flags=state.flags,
    )


@router.post("/responses/stream")
async def responses_stream(body: ResponseRequest, request: Request, ctx: Caller) -> StreamingResponse:
    gw = _gateway(request)
    state = RunState(request_id=uuid.uuid4())
    tenant_id = ctx.require_tenant()
    if body.response_schema is not None:
        raise PolicyDenied(
            "schema_requires_non_streaming", "Structured output is only available without streaming", status=422
        )
    # Policy is checked before the stream starts, so denials are normal HTTP errors.
    slot = gw.slot(tenant_id, state.request_id, int(PURPOSE_DEADLINE_SECONDS[body.purpose]) + 30)
    await slot.__aenter__()
    try:
        prep = await gw.prepare(body, ctx, state)
    except PolicyDenied as exc:
        await slot.__aexit__(None, None, None)
        await gw.persist(
            req=body,
            ctx=ctx,
            state=state,
            status="denied",
            deny_reason=exc.reason,
            stop_reason=None,
            prep=None,
            purpose=body.purpose,
        )
        raise
    except BaseException:
        await slot.__aexit__(None, None, None)
        raise

    async def events() -> AsyncIterator[bytes]:
        stop_reason: str | None = None
        status = "completed"
        try:
            async for ev in gw.stream(prep, state):
                if ev["type"] == "done":
                    stop_reason = ev["stop_reason"]
                if ev["type"] == "error":
                    status = "blocked_output" if ev["code"] == "output_blocked" else "failed"
                yield f"data: {json.dumps(ev)}\n\n".encode()
        finally:
            await slot.__aexit__(None, None, None)
            await gw.persist(
                req=body,
                ctx=ctx,
                state=state,
                status=status,
                deny_reason=None,
                stop_reason=stop_reason,
                prep=prep,
                purpose=body.purpose,
            )

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    )


@router.post("/embeddings", response_model=EmbeddingsOut)
async def embeddings(body: EmbeddingsRequest, request: Request, ctx: Caller) -> EmbeddingsOut:
    gw = _gateway(request)
    state = RunState(request_id=uuid.uuid4())
    try:
        vectors, model = await gw.embed(body, ctx, state)
    except PolicyDenied as exc:
        await gw.persist(
            req=body,
            ctx=ctx,
            state=state,
            status="denied",
            deny_reason=exc.reason,
            stop_reason=None,
            prep=None,
            purpose="embedding",
        )
        raise
    await gw.persist(
        req=body,
        ctx=ctx,
        state=state,
        status="completed",
        deny_reason=None,
        stop_reason="end_turn",
        prep=None,
        purpose="embedding",
    )
    return EmbeddingsOut(vectors=vectors, model=model, dims=len(vectors[0]) if vectors else 0)

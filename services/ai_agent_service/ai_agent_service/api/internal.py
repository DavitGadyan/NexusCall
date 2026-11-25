"""Internal API (docs/BUILD_CONVENTIONS.md §5, ai-agent-service). Service JWT + per-endpoint `azp` allowlist;
tenant and actor from the verified headers. Every endpoint here is read-only with respect to agent
configuration: no internal caller (and no model output) can change prompts, tools or knowledge sources."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from ai_agent_service.drafting import load_runtime, run_drafting, run_postprocess
from ai_agent_service.gateway import AppDeps, tenant_ai_policy
from ai_agent_service.knowledge import enforce_search_rate, rank_score, search
from ai_agent_service.schemas import (
    DraftingIn,
    DraftingOut,
    InternalAgentOut,
    InternalSearchIn,
    InternalSearchOut,
    PostprocessIn,
    PostprocessOut,
    RuntimeConfigOut,
)
from ai_agent_service.settings import (
    AGENT_READERS,
    DRAFTING_CALLERS,
    KNOWLEDGE_SEARCHERS,
    POSTPROCESS_CALLERS,
    RUNTIME_CONFIG_READERS,
)
from ai_agent_service.tables import agents
from nexus_core.authz import InternalContext, require_internal
from nexus_core.errors import NotFound

router = APIRouter(prefix="/internal/v1", tags=["internal"])
_agent_readers = require_internal(*AGENT_READERS)
_runtime_readers = require_internal(*RUNTIME_CONFIG_READERS)
_searchers = require_internal(*KNOWLEDGE_SEARCHERS)
_drafters = require_internal(*DRAFTING_CALLERS)
_postprocessors = require_internal(*POSTPROCESS_CALLERS)


@router.get("/agents/{agent_id}", response_model=InternalAgentOut)
async def get_agent(request: Request, agent_id: uuid.UUID, ctx: InternalContext = Depends(_agent_readers)) -> Any:
    tenant_id = ctx.require_tenant()
    a = agents.c
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        row = (
            await session.execute(
                select(a.id, a.name, a.kind, a.status).where(a.tenant_id == tenant_id, a.id == agent_id)
            )
        ).first()
    if row is None:
        raise NotFound("Agent not found")
    return {"agent_id": row.id, "name": row.name, "kind": row.kind, "status": row.status}


@router.get("/agents/{agent_id}/runtime-config", response_model=RuntimeConfigOut)
async def runtime_config(
    request: Request, agent_id: uuid.UUID, ctx: InternalContext = Depends(_runtime_readers)
) -> Any:
    rt = await load_runtime(AppDeps(request.app), ctx.require_tenant(), ctx.actor, agent_id)
    return {
        "agent_id": rt.agent_id,
        "version": rt.version,
        "kind": rt.kind,
        "system_prompt": rt.system_prompt,
        "model": rt.model,
        "voice_id": rt.extra["voice_id"],
        "tool_ids": rt.tool_ids,
        "greeting": rt.extra["greeting"],
        "disclosure_text": rt.extra["disclosure_text"],
        "escalation_rules": rt.extra["escalation_rules"],
        "max_call_seconds": rt.extra["max_call_seconds"],
    }


@router.post("/knowledge/search", response_model=InternalSearchOut)
async def knowledge_search(request: Request, body: InternalSearchIn, ctx: InternalContext = Depends(_searchers)) -> Any:
    """Agent retrieval: tenant filter and ACL inside the SQL, tenant classification cap, no role-restricted
    sources, rank-derived scores only, rate-limited per tenant."""
    tenant_id = ctx.require_tenant()
    deps = AppDeps(request.app)
    await enforce_search_rate(deps, tenant_id, ctx.actor, per_user=False)
    policy = await tenant_ai_policy(deps, tenant_id)
    hits, _ = await search(
        deps,
        tenant_id=tenant_id,
        actor=ctx.actor,
        query=body.query,
        top_k=body.top_k,
        cap=str(policy.get("data_classification_max", "internal")),
        roles=None,
        source_ids=body.source_ids,
    )
    return {
        "chunks": [
            {
                "chunk_id": h.chunk_id,
                "source_id": h.source_id,
                "source_name": h.source_name,
                "text": h.text,
                "score": rank_score(h.rank, len(hits)),
            }
            for h in hits
        ]
    }


@router.post("/drafting/runs", response_model=DraftingOut)
async def drafting_runs(request: Request, body: DraftingIn, ctx: InternalContext = Depends(_drafters)) -> Any:
    return await run_drafting(
        AppDeps(request.app),
        tenant_id=ctx.require_tenant(),
        actor=ctx.actor,
        thread_id=body.thread_id,
        message_id=body.message_id,
        agent_id=body.agent_id,
        context_blocks=[b.model_dump() for b in body.context_blocks],
    )


@router.post("/calls/postprocess", response_model=PostprocessOut)
async def calls_postprocess(
    request: Request, body: PostprocessIn, ctx: InternalContext = Depends(_postprocessors)
) -> Any:
    return await run_postprocess(
        AppDeps(request.app),
        tenant_id=ctx.require_tenant(),
        actor=ctx.actor,
        call_id=body.call_id,
        contact_id=body.contact_id,
        agent_id=body.agent_id,
        transcript=[t.model_dump() for t in body.transcript],
    )

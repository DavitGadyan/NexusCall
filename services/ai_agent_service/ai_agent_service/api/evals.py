"""Evaluation datasets, cases and runs; drafting provenance (read-only)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_agent_service import idem
from ai_agent_service.deps import audit, job_context, publish, register_job
from ai_agent_service.schemas import (
    CaseCreate,
    CaseOut,
    CasePage,
    DatasetCreate,
    DatasetOut,
    DatasetPage,
    DraftingRunOut,
    DraftingRunPage,
    RunCreate,
    RunOut,
    RunPage,
)
from ai_agent_service.tables import agents, drafting_runs, eval_cases, eval_datasets, eval_runs, prompt_versions
from nexus_core.authz import AuthContext, require_permission
from nexus_core.errors import Conflict, NotFound
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page

router = APIRouter(prefix="/api/v1/ai", tags=["evaluations"])
Reader = Annotated[AuthContext, Depends(require_permission("ai:agents:read"))]
Manager = Annotated[AuthContext, Depends(require_permission("ai:agents:manage"))]
ds, ec, er = eval_datasets.c, eval_cases.c, eval_runs.c


async def _dataset(session: AsyncSession, tenant_id: uuid.UUID, dataset_id: uuid.UUID) -> Any:
    row = (
        (await session.execute(select(eval_datasets).where(ds.tenant_id == tenant_id, ds.id == dataset_id)))
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Dataset not found")
    return row


@router.get("/evals/datasets", response_model=DatasetPage)
async def list_datasets(request: Request, ctx: Reader, limit: Limit = 50, cursor: Cursor = None) -> Any:
    tenant_id = ctx.require_tenant()
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        stmt = select(eval_datasets).where(ds.tenant_id == tenant_id)
        rows = (await session.execute(apply_keyset(stmt, ds.created_at, ds.id, cursor, limit))).mappings().all()
    items, nxt = build_page(list(rows), limit)
    return {"items": items, "next_cursor": nxt}


@router.post("/evals/datasets", response_model=DatasetOut, status_code=201)
async def create_dataset(request: Request, body: DatasetCreate, ctx: Manager) -> Any:
    tenant_id = ctx.require_tenant()
    try:
        async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
            row = (
                (
                    await session.execute(
                        eval_datasets.insert()
                        .values(tenant_id=tenant_id, created_by=str(ctx.actor)[:160], **body.model_dump())
                        .returning(eval_datasets)
                    )
                )
                .mappings()
                .one()
            )
            await audit(session, ctx.actor, "ai.eval.dataset_created", "eval_dataset", row["id"], tenant_id=tenant_id)
            return row
    except IntegrityError as exc:
        raise Conflict("A dataset with this name already exists") from exc


@router.get("/evals/datasets/{dataset_id}", response_model=DatasetOut)
async def get_dataset(request: Request, dataset_id: uuid.UUID, ctx: Reader) -> Any:
    tenant_id = ctx.require_tenant()
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        return await _dataset(session, tenant_id, dataset_id)


@router.get("/evals/datasets/{dataset_id}/cases", response_model=CasePage)
async def list_cases(
    request: Request, dataset_id: uuid.UUID, ctx: Reader, limit: Limit = 50, cursor: Cursor = None
) -> Any:
    tenant_id = ctx.require_tenant()
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        await _dataset(session, tenant_id, dataset_id)
        stmt = select(eval_cases).where(ec.tenant_id == tenant_id, ec.dataset_id == dataset_id)
        rows = (await session.execute(apply_keyset(stmt, ec.created_at, ec.id, cursor, limit))).mappings().all()
    items, nxt = build_page(list(rows), limit)
    return {"items": items, "next_cursor": nxt}


@router.post("/evals/datasets/{dataset_id}/cases", response_model=CaseOut, status_code=201)
async def create_case(request: Request, dataset_id: uuid.UUID, body: CaseCreate, ctx: Manager) -> Any:
    tenant_id = ctx.require_tenant()
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        await _dataset(session, tenant_id, dataset_id)
        return (
            (
                await session.execute(
                    eval_cases.insert()
                    .values(
                        tenant_id=tenant_id,
                        dataset_id=dataset_id,
                        created_by=str(ctx.actor)[:160],
                        **body.model_dump(mode="json"),
                    )
                    .returning(eval_cases)
                )
            )
            .mappings()
            .one()
        )


@router.delete("/evals/datasets/{dataset_id}/cases/{case_id}", status_code=204, response_class=Response)
async def delete_case(request: Request, dataset_id: uuid.UUID, case_id: uuid.UUID, ctx: Manager) -> Response:
    tenant_id = ctx.require_tenant()
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        removed = (
            await session.execute(
                eval_cases.delete()
                .where(ec.tenant_id == tenant_id, ec.dataset_id == dataset_id, ec.id == case_id)
                .returning(ec.id)
            )
        ).first()
    if removed is None:
        raise NotFound("Case not found")
    return Response(status_code=204)


@router.post("/evals/datasets/{dataset_id}/runs", response_model=RunOut, status_code=202)
async def create_run(
    request: Request,
    dataset_id: uuid.UUID,
    body: RunCreate,
    ctx: Manager,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Any:
    """Queue a run of the dataset (plus the built-in injection and poisoning suite) against an agent version."""
    tenant_id = ctx.require_tenant()
    key = idem.require_key(idempotency_key)
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        i = await idem.begin(
            session,
            tenant_id=tenant_id,
            operation=f"evals.runs.create:{dataset_id}",
            key=key,
            payload=body.model_dump(mode="json"),
        )
        if i.replay is not None:
            return i.replay
        await _dataset(session, tenant_id, dataset_id)
        agent = (
            await session.execute(
                select(agents.c.current_version, agents.c.latest_version).where(
                    agents.c.tenant_id == tenant_id, agents.c.id == body.agent_id
                )
            )
        ).first()
        if agent is None:
            raise NotFound("Agent not found")
        version = body.prompt_version or agent.current_version or agent.latest_version
        exists = (
            await session.execute(
                select(prompt_versions.c.id).where(
                    prompt_versions.c.tenant_id == tenant_id,
                    prompt_versions.c.agent_id == body.agent_id,
                    prompt_versions.c.version == version,
                )
            )
        ).first()
        if exists is None:
            raise NotFound("Version not found")
        row = (
            (
                await session.execute(
                    eval_runs.insert()
                    .values(
                        tenant_id=tenant_id,
                        dataset_id=dataset_id,
                        agent_id=body.agent_id,
                        prompt_version=version,
                        include_security_cases=body.include_security_cases,
                        created_by=str(ctx.actor)[:160],
                    )
                    .returning(eval_runs)
                )
            )
            .mappings()
            .one()
        )
        await register_job(session, row["id"], tenant_id, "eval")
        await audit(session, ctx.actor, "ai.eval.run_requested", "eval_run", row["id"], tenant_id=tenant_id)
        out = RunOut.model_validate(dict(row))
        resp = await i.done(202, out.model_dump(mode="json"), row["id"])
    await publish(request.app, "eval", job_context(tenant_id, ctx.actor), row["id"])
    return resp


@router.get("/evals/datasets/{dataset_id}/runs", response_model=RunPage)
async def list_runs(
    request: Request, dataset_id: uuid.UUID, ctx: Reader, limit: Limit = 50, cursor: Cursor = None
) -> Any:
    tenant_id = ctx.require_tenant()
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        await _dataset(session, tenant_id, dataset_id)
        stmt = select(eval_runs).where(er.tenant_id == tenant_id, er.dataset_id == dataset_id)
        rows = (await session.execute(apply_keyset(stmt, er.created_at, er.id, cursor, limit))).mappings().all()
    items, nxt = build_page(list(rows), limit)
    return {"items": items, "next_cursor": nxt}


@router.get("/evals/runs/{run_id}", response_model=RunOut)
async def get_run(request: Request, run_id: uuid.UUID, ctx: Reader) -> Any:
    tenant_id = ctx.require_tenant()
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        row = (
            (await session.execute(select(eval_runs).where(er.tenant_id == tenant_id, er.id == run_id)))
            .mappings()
            .first()
        )
    if row is None:
        raise NotFound("Run not found")
    return row


@router.get("/drafting-runs", response_model=DraftingRunPage)
async def list_drafting_runs(
    request: Request,
    ctx: Reader,
    limit: Limit = 50,
    cursor: Cursor = None,
    thread_id: uuid.UUID | None = None,
) -> Any:
    """Drafting provenance: ids, enums and metrics only (no email or draft text is stored)."""
    tenant_id = ctx.require_tenant()
    dr = drafting_runs.c
    stmt = select(drafting_runs).where(dr.tenant_id == tenant_id)
    if thread_id is not None:
        stmt = stmt.where(dr.thread_id == thread_id)
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        rows = (await session.execute(apply_keyset(stmt, dr.created_at, dr.id, cursor, limit))).mappings().all()
    items, nxt = build_page(list(rows), limit)
    return {"items": items, "next_cursor": nxt}


@router.get("/drafting-runs/{run_id}", response_model=DraftingRunOut)
async def get_drafting_run(request: Request, run_id: uuid.UUID, ctx: Reader) -> Any:
    tenant_id = ctx.require_tenant()
    dr = drafting_runs.c
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        row = (
            (await session.execute(select(drafting_runs).where(dr.tenant_id == tenant_id, dr.id == run_id)))
            .mappings()
            .first()
        )
    if row is None:
        raise NotFound("Drafting run not found")
    return row

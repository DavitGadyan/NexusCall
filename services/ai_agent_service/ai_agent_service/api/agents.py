"""Agents and their append-only prompt versions (`ai:agents:read` / `ai:agents:manage`)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_agent_service import domain, idem
from ai_agent_service.deps import audit
from ai_agent_service.schemas import (
    AgentCreate,
    AgentOut,
    AgentPage,
    AgentPatch,
    VersionIn,
    VersionOut,
    VersionPage,
)
from ai_agent_service.tables import agents, knowledge_sources, prompt_versions
from nexus_core.authz import AuthContext, require_permission
from nexus_core.errors import Conflict, NotFound, Unprocessable
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page

router = APIRouter(prefix="/api/v1/ai/agents", tags=["agents"])
Reader = Annotated[AuthContext, Depends(require_permission("ai:agents:read"))]
Manager = Annotated[AuthContext, Depends(require_permission("ai:agents:manage"))]
a, v = agents.c, prompt_versions.c


def _actor_ref(ctx: AuthContext) -> str:
    return str(ctx.actor)[:160]


async def _get_agent(session: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID, *, lock: bool = False) -> Any:
    stmt = select(agents).where(a.tenant_id == tenant_id, a.id == agent_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).mappings().first()
    if row is None:
        raise NotFound("Agent not found")
    return row


async def _validate_version(session: AsyncSession, tenant_id: uuid.UUID, kind: str, body: VersionIn) -> None:
    bad = domain.invalid_tools(kind, body.tool_ids)
    if bad:
        raise Unprocessable(
            f"Tools not allowed for {kind} agents: {', '.join(bad)}", extra={"code_detail": "tool_not_allowed"}
        )
    if body.knowledge_source_ids:
        found = set(
            (
                await session.execute(
                    select(knowledge_sources.c.id).where(
                        knowledge_sources.c.tenant_id == tenant_id,
                        knowledge_sources.c.id.in_(body.knowledge_source_ids),
                    )
                )
            ).scalars()
        )
        if found != set(body.knowledge_source_ids):
            raise Unprocessable("Unknown knowledge source", extra={"code_detail": "unknown_knowledge_source"})


async def _insert_version(
    session: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID, number: int, body: VersionIn, by: str
) -> Any:
    data = body.model_dump(mode="json")
    data["knowledge_source_ids"] = body.knowledge_source_ids
    return (
        (
            await session.execute(
                prompt_versions.insert()
                .values(tenant_id=tenant_id, agent_id=agent_id, version=number, created_by=by, **data)
                .returning(prompt_versions)
            )
        )
        .mappings()
        .one()
    )


@router.get("", response_model=AgentPage)
async def list_agents(
    request: Request, ctx: Reader, limit: Limit = 50, cursor: Cursor = None, kind: str | None = None
) -> Any:
    tenant_id = ctx.require_tenant()
    stmt = select(agents).where(a.tenant_id == tenant_id)
    if kind in ("voice", "email"):
        stmt = stmt.where(a.kind == kind)
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        rows = (await session.execute(apply_keyset(stmt, a.created_at, a.id, cursor, limit))).mappings().all()
    items, nxt = build_page(list(rows), limit)
    return {"items": items, "next_cursor": nxt}


@router.post("", response_model=AgentOut, status_code=201)
async def create_agent(
    request: Request, body: AgentCreate, ctx: Manager, idempotency_key: Annotated[str | None, Header()] = None
) -> Any:
    tenant_id = ctx.require_tenant()
    key = idem.require_key(idempotency_key)
    try:
        async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
            i = await idem.begin(
                session, tenant_id=tenant_id, operation="agents.create", key=key, payload=body.model_dump(mode="json")
            )
            if i.replay is not None:
                return i.replay
            await _validate_version(session, tenant_id, body.kind, body.config)
            row = (
                (
                    await session.execute(
                        agents.insert()
                        .values(
                            tenant_id=tenant_id,
                            name=body.name,
                            kind=body.kind,
                            description=body.description,
                            latest_version=1,
                            created_by=_actor_ref(ctx),
                        )
                        .returning(agents)
                    )
                )
                .mappings()
                .one()
            )
            await _insert_version(session, tenant_id, row["id"], 1, body.config, _actor_ref(ctx))
            await audit(session, ctx.actor, "ai.agent.created", "agent", row["id"], tenant_id=tenant_id, kind=body.kind)
            out = AgentOut.model_validate(dict(row))
            return await i.done(201, out.model_dump(mode="json"), row["id"])
    except IntegrityError as exc:
        raise Conflict("An agent with this name already exists") from exc


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(request: Request, agent_id: uuid.UUID, ctx: Reader) -> Any:
    tenant_id = ctx.require_tenant()
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        return await _get_agent(session, tenant_id, agent_id)


@router.patch("/{agent_id}", response_model=AgentOut)
async def patch_agent(request: Request, agent_id: uuid.UUID, body: AgentPatch, ctx: Manager) -> Any:
    tenant_id = ctx.require_tenant()
    changes = body.model_dump(exclude_unset=True, exclude={"version"})
    try:
        async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
            row = await _get_agent(session, tenant_id, agent_id, lock=True)
            if row["version"] != body.version:
                raise Conflict("The agent was changed by someone else; reload and retry")
            if row["status"] == "archived":
                raise Conflict("Archived agents cannot be changed")
            updated = (
                (
                    await session.execute(
                        update(agents)
                        .where(a.tenant_id == tenant_id, a.id == agent_id)
                        .values(**changes, version=a.version + 1, updated_at=func.now())
                        .returning(agents)
                    )
                )
                .mappings()
                .one()
            )
            await audit(
                session, ctx.actor, "ai.agent.updated", "agent", agent_id, tenant_id=tenant_id, fields=sorted(changes)
            )
            return updated
    except IntegrityError as exc:
        raise Conflict("An agent with this name already exists") from exc


@router.delete("/{agent_id}", response_model=AgentOut)
async def archive_agent(request: Request, agent_id: uuid.UUID, ctx: Manager) -> Any:
    """Agents are archived, never hard-deleted: their version history is append-only."""
    tenant_id = ctx.require_tenant()
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        await _get_agent(session, tenant_id, agent_id, lock=True)
        updated = (
            (
                await session.execute(
                    update(agents)
                    .where(a.tenant_id == tenant_id, a.id == agent_id)
                    .values(status="archived", version=a.version + 1, updated_at=func.now())
                    .returning(agents)
                )
            )
            .mappings()
            .one()
        )
        await audit(session, ctx.actor, "ai.agent.archived", "agent", agent_id, tenant_id=tenant_id)
        return updated


@router.get("/{agent_id}/versions", response_model=VersionPage)
async def list_versions(
    request: Request, agent_id: uuid.UUID, ctx: Reader, limit: Limit = 50, cursor: Cursor = None
) -> Any:
    tenant_id = ctx.require_tenant()
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        await _get_agent(session, tenant_id, agent_id)
        stmt = select(prompt_versions).where(v.tenant_id == tenant_id, v.agent_id == agent_id)
        rows = (await session.execute(apply_keyset(stmt, v.created_at, v.id, cursor, limit))).mappings().all()
    items, nxt = build_page(list(rows), limit)
    return {"items": items, "next_cursor": nxt}


@router.post("/{agent_id}/versions", response_model=VersionOut, status_code=201)
async def create_version(
    request: Request,
    agent_id: uuid.UUID,
    body: VersionIn,
    ctx: Manager,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Any:
    """Append a new version. It takes effect only when activated."""
    tenant_id = ctx.require_tenant()
    key = idem.require_key(idempotency_key)
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        i = await idem.begin(
            session,
            tenant_id=tenant_id,
            operation=f"agents.versions.create:{agent_id}",
            key=key,
            payload=body.model_dump(mode="json"),
        )
        if i.replay is not None:
            return i.replay
        row = await _get_agent(session, tenant_id, agent_id, lock=True)
        if row["status"] == "archived":
            raise Conflict("Archived agents cannot get new versions")
        await _validate_version(session, tenant_id, row["kind"], body)
        number = int(row["latest_version"]) + 1
        version = await _insert_version(session, tenant_id, agent_id, number, body, _actor_ref(ctx))
        await session.execute(
            update(agents)
            .where(a.tenant_id == tenant_id, a.id == agent_id)
            .values(latest_version=number, version=a.version + 1, updated_at=func.now())
        )
        await audit(
            session, ctx.actor, "ai.agent.version_created", "agent", agent_id, tenant_id=tenant_id, version=number
        )
        out = VersionOut.model_validate(dict(version))
        return await i.done(201, out.model_dump(mode="json"), version["id"])


@router.get("/{agent_id}/versions/{number}", response_model=VersionOut)
async def get_version(request: Request, agent_id: uuid.UUID, number: int, ctx: Reader) -> Any:
    tenant_id = ctx.require_tenant()
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        row = (
            (
                await session.execute(
                    select(prompt_versions).where(v.tenant_id == tenant_id, v.agent_id == agent_id, v.version == number)
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise NotFound("Version not found")
    return row


@router.post("/{agent_id}/versions/{number}/activate", response_model=AgentOut)
async def activate_version(request: Request, agent_id: uuid.UUID, number: int, ctx: Manager) -> Any:
    """Make a version live (audited). Only a human with ai:agents:manage can do this; no internal endpoint
    and no model output can change an agent's configuration (build.md §24.6 rule 12)."""
    tenant_id = ctx.require_tenant()
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        row = await _get_agent(session, tenant_id, agent_id, lock=True)
        if row["status"] == "archived":
            raise Conflict("Archived agents cannot be activated")
        exists = (
            await session.execute(
                select(v.id).where(v.tenant_id == tenant_id, v.agent_id == agent_id, v.version == number)
            )
        ).first()
        if exists is None:
            raise NotFound("Version not found")
        updated = (
            (
                await session.execute(
                    update(agents)
                    .where(a.tenant_id == tenant_id, a.id == agent_id)
                    .values(current_version=number, status="active", version=a.version + 1, updated_at=func.now())
                    .returning(agents)
                )
            )
            .mappings()
            .one()
        )
        await audit(
            session,
            ctx.actor,
            "ai.agent.version_activated",
            "agent",
            agent_id,
            tenant_id=tenant_id,
            version=number,
            previous_version=row["current_version"],
        )
        return updated

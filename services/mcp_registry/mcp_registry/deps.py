from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_registry.policy import DEFAULT_POLICY, ToolPolicy
from mcp_registry.settings import Settings
from mcp_registry.tables import ai_policy_versions, outbox
from nexus_core.audit import emit_audit
from nexus_core.context import Actor
from nexus_core.db import Database

PRODUCER = "mcp-registry"


def db(request: Request) -> Database:
    return request.app.state.db  # type: ignore[no-any-return]


def settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


async def audit(
    session: AsyncSession,
    actor: Actor,
    action: str,
    resource_type: str,
    resource_id: Any = None,
    *,
    tenant_id: uuid.UUID | None,
    outcome: str = "success",
    **metadata: Any,
) -> None:
    await emit_audit(
        session,
        outbox,
        producer=PRODUCER,
        actor=actor,
        tenant_id=tenant_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        metadata=metadata,
    )


async def active_policy(session: AsyncSession) -> tuple[int, ToolPolicy]:
    row = (
        await session.execute(
            select(ai_policy_versions.c.version, ai_policy_versions.c.policy).where(
                ai_policy_versions.c.status == "active"
            )
        )
    ).first()
    if row is None:  # never deployed: the code default (the strictest documented baseline) applies
        return 0, DEFAULT_POLICY
    return int(row.version), ToolPolicy.model_validate(row.policy)


def iso(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    return v

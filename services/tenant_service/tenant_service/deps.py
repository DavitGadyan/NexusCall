from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_core.audit import emit_audit
from nexus_core.authz import AuthContext
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.errors import TooManyRequests
from tenant_service.idp import IdpAdmin
from tenant_service.settings import Settings
from tenant_service.tables import outbox

PRODUCER = "tenant-service"


def db(request: Request) -> Database:
    return request.app.state.db  # type: ignore[no-any-return]


def idp(request: Request) -> IdpAdmin:
    return request.app.state.idp  # type: ignore[no-any-return]


def redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


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


def actor_of(ctx: AuthContext) -> Actor:
    return ctx.actor


async def rate_limit(request: Request, bucket: str, limit: int, window_seconds: int = 60) -> None:
    """Fixed-window limiter keyed by client IP (Traefik sets X-Forwarded-For)."""
    r = redis(request)
    if r is None:
        return
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "unknown"
    )
    key = f"rl:{bucket}:{ip}"
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, window_seconds)
    if count > limit:
        raise TooManyRequests("Too many attempts; try again later")

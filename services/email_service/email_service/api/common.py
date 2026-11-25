"""Shared API helpers: permission dependencies, Idempotency-Key handling, request models, pagination."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from email_service.tables import idempotency_keys
from nexus_core.authz import AuthContext, AuthDep, require_permission
from nexus_core.db import Database
from nexus_core.errors import Forbidden, Unprocessable
from nexus_core.idempotency import IdempotencyStore

MANAGE_PERMISSION = "emails:campaigns:manage"
STORE = IdempotencyStore(idempotency_keys)


class In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Page(BaseModel):
    next_cursor: str | None


def require_any(*permissions: str) -> Callable[..., Awaitable[AuthContext]]:
    async def _dep(ctx: AuthDep) -> AuthContext:
        ctx.require_tenant()
        if not any(ctx.can(p) for p in permissions):
            raise Forbidden(f"Missing permission: one of {', '.join(permissions)}")
        return ctx

    return _dep


Manage = Annotated[AuthContext, Depends(require_permission(MANAGE_PERMISSION))]
CampaignReader = Annotated[AuthContext, Depends(require_any(MANAGE_PERMISSION, "campaigns:read"))]
IdemHeader = Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)]


def db(request: Request) -> Database:
    return request.app.state.db  # type: ignore[no-any-return]


def require_key(key: str | None) -> str:
    if not key:
        raise Unprocessable("This operation requires an Idempotency-Key header")
    return key


@dataclass(slots=True)
class Idem:
    session: AsyncSession
    record_id: uuid.UUID | None
    replay: JSONResponse | None

    async def done(self, status: int, body: Any, resource_id: Any = None) -> Any:
        if self.record_id is not None:
            await STORE.succeed(
                self.session,
                self.record_id,
                status=status,
                body=jsonable_encoder(body),
                resource_id=str(resource_id) if resource_id else None,
            )
        return body


async def idem_begin(
    session: AsyncSession, *, tenant_id: uuid.UUID, operation: str, key: str | None, payload: Any
) -> Idem:
    """The key row is written in the same transaction as the business change: a concurrent duplicate waits on
    the unique constraint and then replays; a failed request rolls the key back with everything else."""
    if key is None:
        return Idem(session, None, None)
    outcome = await STORE.begin(
        session, tenant_id=tenant_id, operation=operation, key=key, payload=jsonable_encoder(payload)
    )
    if not outcome.is_new:
        return Idem(session, None, JSONResponse(outcome.response_body, status_code=outcome.response_status or 200))
    return Idem(session, outcome.record_id, None)


async def idem_peek(
    session: AsyncSession, *, tenant_id: uuid.UUID, operation: str, key: str, payload: Any
) -> JSONResponse | None:
    """Read-only replay check for operations that must do external checks before their write transaction.
    Same key + same payload after success -> stored response; different payload -> 422; in flight -> 409."""
    from sqlalchemy import select

    from nexus_core.errors import Conflict
    from nexus_core.idempotency import canonical_hash

    t = idempotency_keys
    row = (
        (
            await session.execute(
                select(t.c.state, t.c.request_hash, t.c.response_status, t.c.response_body).where(
                    t.c.tenant_id == tenant_id, t.c.operation == operation, t.c.key == key
                )
            )
        )
        .mappings()
        .first()
    )
    if row is None or row["state"] == "failed_retryable":
        return None
    if row["request_hash"] != canonical_hash(jsonable_encoder(payload)):
        raise Unprocessable(
            "Idempotency-Key was reused with a different request payload",
            extra={"code_detail": "idempotency_key_mismatch"},
        )
    if row["state"] == "succeeded":
        return JSONResponse(row["response_body"], status_code=row["response_status"] or 200)
    raise Conflict("A request with this Idempotency-Key is still being processed")


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None

"""Idempotency-Key handling for create endpoints (build.md §5.4).

The key row is inserted in the SAME transaction as the business write: a concurrent duplicate blocks on the
unique constraint and then replays the committed response; a failed request rolls the key back."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ai_agent_service.tables import idempotency_keys
from nexus_core.errors import Unprocessable
from nexus_core.idempotency import IdempotencyStore

STORE = IdempotencyStore(idempotency_keys)


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


def require_key(key: str | None) -> str:
    if not key:
        raise Unprocessable("This operation requires an Idempotency-Key header")
    return key


async def begin(session: AsyncSession, *, tenant_id: uuid.UUID, operation: str, key: str, payload: Any) -> Idem:
    outcome = await STORE.begin(
        session, tenant_id=tenant_id, operation=operation, key=key, payload=jsonable_encoder(payload)
    )
    if not outcome.is_new:
        return Idem(session, None, JSONResponse(outcome.response_body, status_code=outcome.response_status or 200))
    return Idem(session, outcome.record_id, None)

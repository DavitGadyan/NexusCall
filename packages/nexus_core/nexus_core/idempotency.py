"""Idempotency keys scoped by (tenant, operation, key) with request-hash conflict detection.

Uniqueness is enforced by PostgreSQL (INSERT ... ON CONFLICT), never by a
pre-check alone, so concurrent duplicates cannot both proceed.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Table, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_core.errors import Conflict, Unprocessable


def canonical_hash(payload: Any) -> str:
    """SHA-256 over deterministic canonical JSON (sorted keys, no whitespace)."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class IdempotencyOutcome:
    is_new: bool
    record_id: uuid.UUID
    response_status: int | None = None
    response_body: Any = None
    resource_id: str | None = None


class IdempotencyStore:
    def __init__(self, table: Table) -> None:
        self.t = table

    async def begin(
        self, session: AsyncSession, *, tenant_id: uuid.UUID, operation: str, key: str, payload: Any
    ) -> IdempotencyOutcome:
        if not key or len(key) > 255:
            raise Unprocessable("Idempotency-Key header must be 1..255 characters")
        request_hash = canonical_hash(payload)
        record_id = uuid.uuid4()
        stmt = (
            insert(self.t)
            .values(
                id=record_id,
                tenant_id=tenant_id,
                operation=operation,
                key=key,
                request_hash=request_hash,
                state="processing",
            )
            .on_conflict_do_nothing(constraint="uq_idempotency_scope")
            .returning(self.t.c.id)
        )
        inserted = (await session.execute(stmt)).scalar_one_or_none()
        if inserted is not None:
            return IdempotencyOutcome(is_new=True, record_id=inserted)

        existing = (
            (
                await session.execute(
                    select(self.t)
                    .where(self.t.c.tenant_id == tenant_id, self.t.c.operation == operation, self.t.c.key == key)
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        if existing["request_hash"] != request_hash:
            raise Unprocessable(
                "Idempotency-Key was reused with a different request payload",
                extra={"code_detail": "idempotency_key_mismatch"},
            )
        if existing["state"] == "succeeded":
            return IdempotencyOutcome(
                False, existing["id"], existing["response_status"], existing["response_body"], existing["resource_id"]
            )
        if existing["state"] == "failed_retryable":
            await session.execute(update(self.t).where(self.t.c.id == existing["id"]).values(state="processing"))
            return IdempotencyOutcome(is_new=True, record_id=existing["id"])
        raise Conflict("A request with this Idempotency-Key is still being processed")

    async def succeed(
        self, session: AsyncSession, record_id: uuid.UUID, *, status: int, body: Any, resource_id: str | None = None
    ) -> None:
        await session.execute(
            update(self.t)
            .where(self.t.c.id == record_id)
            .values(state="succeeded", response_status=status, response_body=body, resource_id=resource_id)
        )

    async def fail_retryable(self, session: AsyncSession, record_id: uuid.UUID) -> None:
        await session.execute(update(self.t).where(self.t.c.id == record_id).values(state="failed_retryable"))

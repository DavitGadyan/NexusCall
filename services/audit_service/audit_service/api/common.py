"""Shared filters, response models and self-audit helpers for the audit API."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select

from audit_service.chain import AuditRecord, append
from audit_service.tables import audit_events
from nexus_core.context import Actor, correlation_id_var, request_id_var
from nexus_core.db import Database
from nexus_core.errors import ServiceUnavailable, Unprocessable
from nexus_core.storage import ObjectStore

PRODUCER = "audit-service"
Outcome = Literal["success", "denied", "failure"]
_ID = r"^[A-Za-z0-9._:@-]{1,128}$"


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    utc = _utc(value)
    return utc.isoformat() if utc else None


@dataclass(frozen=True, slots=True)
class AuditFilters:
    action: str | None = None
    actor_id: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    outcome: str | None = None
    from_: datetime | None = None
    to: datetime | None = None

    def __post_init__(self) -> None:
        if self.from_ and self.to and _utc(self.from_) > _utc(self.to):  # type: ignore[operator]
            raise Unprocessable("'from' must not be after 'to'")

    def apply(self, stmt: Select[Any]) -> Select[Any]:
        c = audit_events.c
        if self.action:
            stmt = stmt.where(c.action.startswith(self.action, autoescape=True))
        if self.actor_id:
            stmt = stmt.where(c.actor_id == self.actor_id)
        if self.resource_type:
            stmt = stmt.where(c.resource_type == self.resource_type)
        if self.resource_id:
            stmt = stmt.where(c.resource_id == self.resource_id)
        if self.outcome:
            stmt = stmt.where(c.outcome == self.outcome)
        if self.from_:
            stmt = stmt.where(c.occurred_at >= _utc(self.from_))
        if self.to:
            stmt = stmt.where(c.occurred_at <= _utc(self.to))
        return stmt

    def describe(self) -> dict[str, Any]:
        """Filters as safe audit metadata (identifiers and enums only)."""
        out: dict[str, Any] = {
            "action_prefix": self.action,
            "actor_id": self.actor_id,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "outcome": self.outcome,
            "from": _iso(self.from_),
            "to": _iso(self.to),
        }
        return {k: v for k, v in out.items() if v is not None}


def audit_filters(
    action: Annotated[
        str | None, Query(max_length=128, pattern=r"^[a-z0-9_.:-]+$", description="Action prefix")
    ] = None,
    actor_id: Annotated[str | None, Query(pattern=_ID)] = None,
    resource_type: Annotated[str | None, Query(max_length=64, pattern=r"^[a-z0-9_.:-]+$")] = None,
    resource_id: Annotated[str | None, Query(pattern=_ID)] = None,
    outcome: Outcome | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
) -> AuditFilters:
    return AuditFilters(action, actor_id, resource_type, resource_id, outcome, from_, to)


class FilterBody(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    action: str | None = Field(None, max_length=128, pattern=r"^[a-z0-9_.:-]+$")
    actor_id: str | None = Field(None, pattern=_ID)
    resource_type: str | None = Field(None, max_length=64, pattern=r"^[a-z0-9_.:-]+$")
    resource_id: str | None = Field(None, pattern=_ID)
    outcome: Outcome | None = None
    from_: datetime | None = Field(None, alias="from")
    to: datetime | None = None

    def filters(self) -> AuditFilters:
        return AuditFilters(
            self.action, self.actor_id, self.resource_type, self.resource_id, self.outcome, self.from_, self.to
        )


class AuditEventOut(BaseModel):
    id: uuid.UUID
    occurred_at: datetime
    actor_type: str
    actor_id: str
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    request_id: str | None
    correlation_id: uuid.UUID | None
    producer: str
    metadata: dict[str, Any]


class PlatformAuditEventOut(AuditEventOut):
    tenant_id: uuid.UUID | None


class AuditPage(BaseModel):
    items: list[AuditEventOut]
    next_cursor: str | None


class PlatformAuditPage(BaseModel):
    items: list[PlatformAuditEventOut]
    next_cursor: str | None


LIST_COLUMNS = (
    audit_events.c.id,
    audit_events.c.tenant_id,
    audit_events.c.occurred_at,
    audit_events.c.actor_type,
    audit_events.c.actor_id,
    audit_events.c.action,
    audit_events.c.resource_type,
    audit_events.c.resource_id,
    audit_events.c.outcome,
    audit_events.c.request_id,
    audit_events.c.correlation_id,
    audit_events.c.producer,
    audit_events.c.metadata,
)


def db(request: Request) -> Database:
    return request.app.state.db  # type: ignore[no-any-return]


def platform_db(request: Request) -> Database:
    database: Database | None = getattr(request.app.state, "platform_db", None)
    if database is None:
        raise ServiceUnavailable("The platform audit read path is not configured")
    return database


def object_store(request: Request) -> ObjectStore:
    store: ObjectStore | None = getattr(request.app.state, "object_store", None)
    if store is None:
        store = ObjectStore.from_settings(request.app.state.settings)
        request.app.state.object_store = store
    return store


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


async def record_self(
    database: Database,
    *,
    actor: Actor,
    tenant_id: uuid.UUID | None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    outcome: str = "success",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Audit-service's own actions (searches, exports, verifications) go straight into the chain."""
    record = AuditRecord(
        event_id=uuid.uuid4(),
        tenant_id=tenant_id,
        occurred_at=datetime.now(UTC),
        actor_type=actor.type if actor.type in ("user", "service", "system") else "system",
        actor_id=actor.id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        request_id=request_id_var.get(),
        correlation_id=_uuid_or_none(correlation_id_var.get()),
        causation_id=None,
        producer=PRODUCER,
        metadata=metadata or {},
    )
    ctx = database.tenant_session(tenant_id, actor) if tenant_id else database.global_session(actor)
    async with ctx as session:
        await append(session, record)

"""Event envelope (build.md §6.3). Payloads carry identifiers and minimal metadata only:
never message bodies, transcripts, prompts or secret values."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ActorRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["user", "service", "system"]
    id: str = Field(max_length=128)


class EventEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=128)
    event_version: int = Field(default=1, ge=1)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    tenant_id: uuid.UUID | None = None
    correlation_id: uuid.UUID
    causation_id: uuid.UUID | None = None
    producer: str = Field(max_length=64)
    actor: ActorRef
    payload: dict[str, Any] = Field(default_factory=dict)


def make_event(
    event_type: str,
    *,
    producer: str,
    actor: ActorRef,
    correlation_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None,
    payload: BaseModel | dict[str, Any],
    causation_id: uuid.UUID | str | None = None,
    event_version: int = 1,
) -> EventEnvelope:
    from nexus_events.registry import payload_model_for

    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    model = payload_model_for(event_type, event_version)
    if model is not None:
        data = model.model_validate(data).model_dump(mode="json")
    return EventEnvelope(
        event_type=event_type,
        event_version=event_version,
        tenant_id=uuid.UUID(str(tenant_id)) if tenant_id else None,
        correlation_id=uuid.UUID(str(correlation_id)),
        causation_id=uuid.UUID(str(causation_id)) if causation_id else None,
        producer=producer,
        actor=actor,
        payload=data,
    )

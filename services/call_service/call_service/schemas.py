"""Request/response models (extra="forbid" on input; explicit response models, never raw rows)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from call_service.gates import is_e164
from nexus_core.errors import Unprocessable


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- internal (BUILD_CONVENTIONS §5, call-service)
class CreateCallIn(In):
    contact_id: uuid.UUID
    agent_id: uuid.UUID
    campaign_id: uuid.UUID | None = None
    campaign_contact_id: uuid.UUID | None = None
    scheduled_for: datetime | None = None

    @field_validator("scheduled_for")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("scheduled_for must include a timezone (ISO-8601 UTC)")
        return v


class CreateCallOut(BaseModel):
    call_id: uuid.UUID
    state: str


class ReasonIn(In):
    reason: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_:.-]*$")


class CancelOut(BaseModel):
    call_id: uuid.UUID
    state: str


class TransferOut(BaseModel):
    status: Literal["transferring", "unavailable"]


class InternalCallOut(BaseModel):
    call_id: uuid.UUID
    state: str
    direction: str
    contact_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None
    campaign_id: uuid.UUID | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


class OptOutOut(BaseModel):
    call_id: uuid.UUID
    opted_out: bool


class AckOut(BaseModel):
    call_id: uuid.UUID
    state: str


# ---------------------------------------------------------------- public
class CallOut(BaseModel):
    id: uuid.UUID
    direction: str
    state: str
    contact_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    campaign_id: uuid.UUID | None
    inbound_number_id: uuid.UUID | None
    # Masked: full numbers stay server-side (PII minimization in list views).
    from_masked: str | None
    to_masked: str | None
    is_test: bool
    scheduled_for: datetime | None
    attempt_count: int
    initiated_at: datetime | None
    answered_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    end_reason: str | None
    blocked_reason: str | None
    handoff_state: str | None
    cost_micros: int
    opted_out: bool
    outcome: str | None = None
    qualified: bool | None = None
    created_at: datetime
    updated_at: datetime


class CallPage(BaseModel):
    items: list[CallOut]
    next_cursor: str | None


class HangupOut(BaseModel):
    call_id: uuid.UUID
    state: str
    hangup: Literal["requested", "canceled", "not_live"]


class CallTestIn(In):
    contact_id: uuid.UUID
    agent_id: uuid.UUID


def mask_e164(value: str | None) -> str | None:
    if not value:
        return None
    return value[:3] + "*" * max(0, len(value) - 5) + value[-2:]


def call_out(row: dict[str, Any], outcome: dict[str, Any] | None = None) -> CallOut:
    return CallOut(
        **{k: row[k] for k in CallOut.model_fields if k in row and k not in ("from_masked", "to_masked")},
        from_masked=mask_e164(row.get("from_e164")),
        to_masked=mask_e164(row.get("to_e164")),
        outcome=(outcome or {}).get("outcome"),
        qualified=(outcome or {}).get("qualified"),
    )


class InboundNumberIn(In):
    e164: str = Field(min_length=8, max_length=16)
    agent_id: uuid.UUID
    label: str = Field(default="", max_length=120)
    provider_number_sid: str | None = Field(default=None, pattern=r"^PN[0-9a-fA-F]{32}$")
    use_for_outbound: bool = True
    business_hours_only: bool = False
    handoff_e164: str | None = Field(default=None, max_length=16)

    @field_validator("e164", "handoff_e164")
    @classmethod
    def _e164(cls, v: str | None) -> str | None:
        if v is not None and not is_e164(v):
            raise ValueError("must be an E.164 phone number")
        return v


class InboundNumberPatch(In):
    agent_id: uuid.UUID | None = None
    label: str | None = Field(default=None, max_length=120)
    status: Literal["active", "disabled"] | None = None
    use_for_outbound: bool | None = None
    business_hours_only: bool | None = None
    handoff_e164: str | None = Field(default=None, max_length=16)
    expected_version: int

    @field_validator("handoff_e164")
    @classmethod
    def _e164(cls, v: str | None) -> str | None:
        if v is not None and not is_e164(v):
            raise ValueError("must be an E.164 phone number")
        return v


class InboundNumberOut(BaseModel):
    id: uuid.UUID
    e164: str
    provider_number_sid: str | None
    agent_id: uuid.UUID
    label: str
    status: str
    use_for_outbound: bool
    business_hours_only: bool
    handoff_e164: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class InboundNumberPage(BaseModel):
    items: list[InboundNumberOut]
    next_cursor: str | None


class NotContactable(Unprocessable):
    code, title = "not_contactable", "Contact is not contactable"

"""Request/response models (extra="forbid" on every input)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal
from zoneinfo import available_timezones

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_TZ = frozenset(available_timezones())
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
EDITABLE_WHEN_PAUSED = frozenset(
    {"window_start", "window_end", "window_days", "max_concurrency", "calls_per_minute", "end_at", "description"}
)


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CampaignFields(In):
    description: str | None = Field(default=None, max_length=1000)
    agent_id: uuid.UUID | None = None
    segment_id: uuid.UUID | None = None
    timezone: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    window_days: list[Day] | None = Field(default=None, min_length=1, max_length=7)
    max_concurrency: int | None = Field(default=None, ge=1, le=1000)
    calls_per_minute: int | None = Field(default=None, ge=1, le=600)
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    retry_delay_seconds: int | None = Field(default=None, ge=60, le=604_800)
    start_at: datetime | None = None
    end_at: datetime | None = None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        if v is not None and v not in _TZ:
            raise ValueError("unknown IANA timezone")
        return v

    @field_validator("window_start", "window_end")
    @classmethod
    def _hhmm(cls, v: str | None) -> str | None:
        if v is not None and not _HHMM.match(v):
            raise ValueError("expected HH:MM")
        return v

    @field_validator("window_days")
    @classmethod
    def _days(cls, v: list[str] | None) -> list[str] | None:
        return sorted(set(v), key=["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index) if v else v

    @field_validator("start_at", "end_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("timestamps must include a timezone (ISO-8601 UTC)")
        return v

    @model_validator(mode="after")
    def _order(self) -> CampaignFields:
        if self.window_start and self.window_end and self.window_start >= self.window_end:
            raise ValueError("window_start must be before window_end")
        if self.start_at and self.end_at and self.start_at >= self.end_at:
            raise ValueError("start_at must be before end_at")
        return self


class CampaignIn(CampaignFields):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v or _CONTROL.search(v):
            raise ValueError("invalid name")
        return v


class CampaignPatch(CampaignFields):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    expected_version: int


class ActionIn(In):
    expected_version: int | None = None
    reason: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9_ -]+$")


class CampaignOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    agent_id: uuid.UUID | None
    segment_id: uuid.UUID | None
    state: str
    timezone: str
    window_start: str
    window_end: str
    window_days: list[str]
    max_concurrency: int
    calls_per_minute: int
    max_attempts: int
    retry_delay_seconds: int
    start_at: datetime | None
    end_at: datetime | None
    snapshot_status: str
    snapshot_total: int
    failure_reason: str | None
    launched_at: datetime | None
    completed_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime
    allowed_actions: list[str] = Field(default_factory=list)


class CampaignPage(BaseModel):
    items: list[CampaignOut]
    next_cursor: str | None


class CampaignContactOut(BaseModel):
    id: uuid.UUID
    contact_id: uuid.UUID
    state: str
    attempts: int
    next_attempt_at: datetime | None
    last_call_id: uuid.UUID | None
    outcome: str | None
    skip_reason: str | None
    created_at: datetime
    updated_at: datetime


class ContactPage(BaseModel):
    items: list[CampaignContactOut]
    next_cursor: str | None


class ReportOut(BaseModel):
    campaign_id: uuid.UUID
    state: str
    snapshot_status: str
    audience: int
    contacts_by_state: dict[str, int]
    outcomes: dict[str, int]
    attempts: int
    calls_dispatched: int


class KillSwitchIn(In):
    engaged: bool
    reason: str | None = Field(default=None, max_length=300)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str | None) -> str | None:
        if v is not None and _CONTROL.search(v):
            raise ValueError("control characters are not allowed")
        return v


class KillSwitchOut(BaseModel):
    scope: str
    channel: str
    engaged: bool
    reason: str | None
    changed_by: str | None
    updated_at: datetime | None


def campaign_out(row: Any) -> CampaignOut:
    from campaign_service.state_machine import allowed_actions

    data = {k: row[k] for k in CampaignOut.model_fields if k != "allowed_actions"}
    return CampaignOut(**data, allowed_actions=allowed_actions(row["state"]))

"""Pydantic request/response models for the public API (extra="forbid" on every input)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar
from zoneinfo import available_timezones

from pydantic import BaseModel, ConfigDict, Field, field_validator

_TZ = frozenset(available_timezones())
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
T = TypeVar("T")

LeadStatus = Literal["new", "contacted", "qualified", "unqualified", "booked", "won", "lost"]
ContactStatus = Literal["active", "inactive"]
Channel = Literal["phone", "email"]
LawfulBasis = Literal["consent", "legitimate_interest", "contract", "legal_obligation", "vital_interest", "public_task"]


def clean_text(value: str | None) -> str | None:
    """Short free-text fields: trimmed, no control characters (CR/LF/NUL)."""
    if value is None:
        return None
    value = value.strip()
    if _CONTROL.search(value):
        raise ValueError("control characters are not allowed")
    return value or None


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContactFields(In):
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    company: str | None = Field(default=None, max_length=200)
    organization_id: uuid.UUID | None = None
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=32, description="Any format; stored as E.164")
    timezone: str | None = Field(default=None, max_length=64)
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    locale: str | None = Field(default=None, pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    owner_id: uuid.UUID | None = None
    source: str | None = Field(default=None, max_length=64)
    custom_fields: dict[str, Any] | None = None

    @field_validator("first_name", "last_name", "company", "email", "phone", "source")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        return clean_text(v)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        if v is not None and v not in _TZ:
            raise ValueError("unknown IANA timezone")
        return v


class ContactIn(ContactFields):
    status: ContactStatus = "active"
    tag_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)


class ContactPatch(ContactFields):
    status: ContactStatus | None = None
    expected_version: int


class ContactOut(BaseModel):
    id: uuid.UUID
    first_name: str | None
    last_name: str | None
    company: str | None
    organization_id: uuid.UUID | None
    email: str | None
    phone_e164: str | None
    timezone: str | None
    country: str | None
    locale: str | None
    status: str
    owner_id: uuid.UUID | None
    source: str | None
    custom_fields: dict[str, Any]
    tags: list[str]
    version: int
    created_at: datetime
    updated_at: datetime


class ContactPage(BaseModel):
    items: list[ContactOut]
    next_cursor: str | None


class ContactSummary(BaseModel):
    id: uuid.UUID
    first_name: str | None
    last_name: str | None
    company: str | None
    email: str | None
    phone_e164: str | None
    status: str


class LeadFields(In):
    status: LeadStatus | None = None
    source: str | None = Field(default=None, max_length=64)
    campaign_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    score: int | None = Field(default=None, ge=0, le=100)
    qualified: bool | None = None

    @field_validator("source")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        return clean_text(v)


class LeadIn(LeadFields):
    contact_id: uuid.UUID | None = None
    contact: ContactIn | None = None


class LeadPatch(LeadFields):
    expected_version: int


class LeadOut(BaseModel):
    id: uuid.UUID
    contact_id: uuid.UUID
    status: str
    source: str | None
    campaign_id: uuid.UUID | None
    call_id: uuid.UUID | None
    booking_id: uuid.UUID | None
    owner_id: uuid.UUID | None
    score: int | None
    qualified: bool | None
    last_outcome: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    contact: ContactSummary | None = None


class LeadPage(BaseModel):
    items: list[LeadOut]
    next_cursor: str | None


class ConsentIn(In):
    channel: Channel
    status: Literal["granted", "withdrawn"]
    lawful_basis: LawfulBasis = "consent"
    source: str = Field(min_length=1, max_length=64)
    source_ref: str | None = Field(default=None, max_length=128)
    captured_at: datetime | None = None
    policy_version: str | None = Field(default=None, max_length=32)
    evidence_ref: str | None = Field(default=None, max_length=255)

    @field_validator("source", "source_ref", "policy_version", "evidence_ref")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        return clean_text(v)


class ConsentOut(BaseModel):
    id: uuid.UUID
    contact_id: uuid.UUID
    channel: str
    status: str
    lawful_basis: str
    source: str
    source_ref: str | None
    captured_at: datetime
    policy_version: str | None
    evidence_ref: str | None
    recorded_by: str
    created_at: datetime


class NoteIn(In):
    text: str = Field(min_length=1, max_length=2000)


class NoteOut(BaseModel):
    id: uuid.UUID
    contact_id: uuid.UUID
    text: str
    source_type: str
    source_id: str | None
    created_by: str
    created_at: datetime


class ChannelState(BaseModel):
    allowed: bool
    reasons: list[str]


class ContactabilityOut(BaseModel):
    contact_id: uuid.UUID
    phone: ChannelState
    email: ChannelState


class Items(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None

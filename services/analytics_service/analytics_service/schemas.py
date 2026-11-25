"""Response models (BUILD_CONVENTIONS §3 API). Money is integer micros of USD; durations are seconds."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class CallsOut(_Out):
    attempted: int
    answered: int
    ended: int
    completed: int
    failed: int
    canceled: int
    opted_out: int
    qualified: int
    answer_rate: float | None
    total_duration_seconds: int
    average_duration_seconds: int | None


class BookingsOut(_Out):
    created: int
    confirmed: int
    canceled: int


class EmailsOut(_Out):
    accepted: int
    delivered: int
    bounced: int
    complained: int
    rejected: int
    failed: int
    unsubscribed: int
    delivery_rate: float | None
    bounce_rate: float | None
    complaint_rate: float | None


class DraftsOut(_Out):
    requested: int
    generated: int
    approved: int
    edited: int
    rejected: int
    sent: int
    approved_rate: float | None
    rejected_rate: float | None
    edited_rate: float | None


class CostsOut(_Out):
    call_cost_micros: int
    cost_per_call_micros: int | None
    email_cost_micros: int
    cost_per_email_micros: int | None
    email_cost_estimated: bool
    ai_cost_micros: int
    cost_per_ai_interaction_micros: int | None
    ai_input_tokens: int
    ai_output_tokens: int
    total_cost_micros: int


class Count(_Out):
    created: int | None = None
    launched: int | None = None


class OverviewOut(_Out):
    from_: date = Field(alias="from")
    to: date
    calls: CallsOut
    bookings: BookingsOut
    emails: EmailsOut
    drafts: DraftsOut
    leads: Count
    campaigns: Count
    costs: CostsOut


class Point(_Out):
    day: date
    value: int


class TimeseriesOut(_Out):
    metric: str
    dimension: str
    from_: date = Field(alias="from")
    to: date
    points: list[Point]


class MetricsOut(_Out):
    items: list[str]


class CampaignCallsOut(_Out):
    attempted: int
    answered: int
    ended: int
    completed: int
    failed: int
    opted_out: int
    qualified: int
    answer_rate: float | None
    average_duration_seconds: int | None
    cost_micros: int
    cost_per_call_micros: int | None


class CampaignEmailsOut(_Out):
    messages: int
    accepted: int
    delivered: int
    bounced: int
    complained: int
    unsubscribed: int
    delivery_rate: float | None


class CampaignOut(_Out):
    campaign_id: uuid.UUID
    channel: str | None
    launched_at: datetime | None
    calls: CampaignCallsOut
    emails: CampaignEmailsOut


class ExportOut(_Out):
    id: uuid.UUID
    kind: str
    status: str
    params: dict[str, Any]
    row_count: int | None
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None
    download_url: str | None = None
    expires_in_seconds: int | None = None


class ExportPage(_Out):
    items: list[ExportOut]
    next_cursor: str | None


class TenantUsageOut(_Out):
    tenant_id: uuid.UUID
    calls: int
    call_seconds: int
    call_cost_micros: int
    emails_accepted: int
    email_cost_micros: int
    ai_requests: int
    ai_input_tokens: int
    ai_output_tokens: int
    ai_cost_micros: int
    total_cost_micros: int


class UsageTotalsOut(_Out):
    tenants: int
    calls: int
    call_seconds: int
    call_cost_micros: int
    emails_accepted: int
    email_cost_micros: int
    ai_requests: int
    ai_cost_micros: int
    total_cost_micros: int


class UsageOut(_Out):
    from_: date = Field(alias="from")
    to: date
    email_cost_estimated: bool
    items: list[TenantUsageOut]
    totals: UsageTotalsOut

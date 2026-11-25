"""Typed event payloads. IDs + small enums only (no PII bodies, no secrets)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _P(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuditEventPayload(_P):
    action: str = Field(max_length=128)
    resource_type: str = Field(max_length=64)
    resource_id: str | None = Field(default=None, max_length=128)
    outcome: Literal["success", "denied", "failure"] = "success"
    request_id: str | None = None
    # Safe metadata only. Producers must not put tokens, passwords or message bodies here.
    metadata: dict[str, Any] = Field(default_factory=dict)


class TenantRef(_P):
    tenant_id: uuid.UUID
    slug: str | None = None


class TenantStatusChanged(_P):
    tenant_id: uuid.UUID
    status: Literal["active", "suspended", "archived"]


class MembershipChanged(_P):
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    change: Literal["added", "removed", "roles_changed", "deactivated", "reactivated"]


class InvitationRef(_P):
    invitation_id: uuid.UUID
    tenant_id: uuid.UUID | None = None


class SettingsChanged(_P):
    scope: Literal["platform", "tenant"]
    section: str
    version: int


class ContactRef(_P):
    contact_id: uuid.UUID
    lead_id: uuid.UUID | None = None


class SuppressionAdded(_P):
    suppression_id: uuid.UUID
    channel: Literal["phone", "email", "all"]
    reason: str
    contact_id: uuid.UUID | None = None


class ImportCompleted(_P):
    import_id: uuid.UUID
    total: int
    created: int
    updated: int
    failed: int


class CampaignRef(_P):
    campaign_id: uuid.UUID
    channel: Literal["call", "email"] = "call"


class KillSwitch(_P):
    scope: Literal["tenant", "campaign", "platform"]
    channel: Literal["call", "email", "all"]
    engaged: bool
    campaign_id: uuid.UUID | None = None


class CallRef(_P):
    call_id: uuid.UUID


class CallStateChanged(_P):
    call_id: uuid.UUID
    from_state: str | None
    to_state: str
    direction: Literal["inbound", "outbound"]
    campaign_id: uuid.UUID | None = None


class CallCompleted(_P):
    call_id: uuid.UUID
    direction: Literal["inbound", "outbound"]
    final_state: str
    duration_seconds: int = 0
    campaign_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    cost_micros: int = 0


class CallOptedOut(_P):
    call_id: uuid.UUID
    contact_id: uuid.UUID | None


class VoiceSessionEnded(_P):
    session_id: uuid.UUID
    call_id: uuid.UUID
    reason: str
    turns: int = 0


class CallOutcomeExtracted(_P):
    call_id: uuid.UUID
    outcome: str
    qualified: bool | None = None
    contact_id: uuid.UUID | None = None


class BookingRef(_P):
    booking_id: uuid.UUID
    contact_id: uuid.UUID | None = None
    status: str


class EmailMessageStateChanged(_P):
    message_id: uuid.UUID
    state: str
    category: Literal["campaign", "transactional", "reply"]
    campaign_id: uuid.UUID | None = None


class EmailAddressEvent(_P):
    # Hash of the normalized address so downstream consumers can match without PII.
    address_hash: str = Field(min_length=64, max_length=64)
    contact_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None


class InboundMessageRef(_P):
    message_id: uuid.UUID
    thread_id: uuid.UUID
    mailbox_id: uuid.UUID


class DraftRef(_P):
    draft_id: uuid.UUID
    thread_id: uuid.UUID
    version: int | None = None


class ConnectionChanged(_P):
    connection_id: uuid.UUID
    provider: str
    status: str


class WebhookDeliveryFailed(_P):
    endpoint_id: uuid.UUID
    delivery_id: uuid.UUID
    attempts: int


class AiRequestCompleted(_P):
    request_id: uuid.UUID
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_micros: int
    latency_ms: int
    purpose: str


class AiSecurityEvent(_P):
    kind: str
    agent_id: uuid.UUID | None = None
    tool_id: str | None = None
    detail: str = Field(default="", max_length=500)


class AiBudgetExceeded(_P):
    scope: Literal["tenant", "agent"]
    period: str
    limit_micros: int
    used_micros: int


class NotificationRequested(_P):
    template: str
    recipient_user_id: uuid.UUID | None = None
    # For invitations etc: notification-service resolves the address from the referenced resource.
    resource_type: str | None = None
    resource_id: uuid.UUID | None = None
    channel: Literal["in_app", "email"] = "in_app"
    occurred_at: datetime | None = None

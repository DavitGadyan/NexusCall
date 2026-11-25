"""Request and response models. Requests forbid unknown fields; responses never carry secrets or raw MIME."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------------------------------- mailboxes
class MailboxIn(_In):
    address: str = Field(min_length=3, max_length=320)
    display_name: str = Field(default="", max_length=200)
    agent_id: uuid.UUID | None = None
    auto_draft: bool = True


class MailboxPatch(_In):
    display_name: str | None = Field(default=None, max_length=200)
    agent_id: uuid.UUID | None = None
    auto_draft: bool | None = None
    status: Literal["active", "disabled"] | None = None
    expected_version: int


class MailboxOut(_Out):
    id: uuid.UUID
    address: str
    display_name: str
    agent_id: uuid.UUID | None
    auto_draft: bool
    status: str
    version: int
    created_at: datetime
    updated_at: datetime


class MailboxPage(BaseModel):
    items: list[MailboxOut]
    next_cursor: str | None


# ------------------------------------------------------------------------------------------- inbox
class ThreadOut(_Out):
    id: uuid.UUID
    mailbox_id: uuid.UUID
    subject: str
    status: str
    priority: str
    intent: str | None
    assigned_to: str | None
    contact_id: uuid.UUID | None
    campaign_id: uuid.UUID | None
    message_count: int
    last_message_at: datetime
    created_at: datetime


class ThreadPage(BaseModel):
    items: list[ThreadOut]
    next_cursor: str | None


class AttachmentOut(_Out):
    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    scan_status: str
    quarantined: bool
    rejection_reason: str | None
    downloadable: bool


class MessageOut(_Out):
    id: uuid.UUID
    direction: str
    from_address: str
    from_name: str
    to_addresses: list[str]
    cc_addresses: list[str]
    subject: str
    text_body: str
    # Sanitized server-side (bleach allowlist). Clients must still render it sandboxed (no scripts).
    html_sanitized: str | None
    auto_flags: list[str]
    received_at: datetime
    draft_id: uuid.UUID | None
    draft_version: int | None
    content_purged: bool
    attachments: list[AttachmentOut] = Field(default_factory=list)


class DraftBrief(_Out):
    id: uuid.UUID
    status: str
    current_version: int
    approved_version: int | None
    escalated: bool


class ThreadDetail(ThreadOut):
    messages: list[MessageOut]
    drafts: list[DraftBrief]


class DownloadOut(BaseModel):
    url: str
    expires_in_seconds: int


# ------------------------------------------------------------------------------------------- drafts
class DraftOut(_Out):
    id: uuid.UUID
    thread_id: uuid.UUID
    message_id: uuid.UUID
    mailbox_id: uuid.UUID
    subject: str
    status: str
    current_version: int
    approved_version: int | None
    approved_by: str | None
    approved_at: datetime | None
    assigned_to: str | None
    escalated: bool
    escalation_reason: str | None
    intent: str | None
    priority: str
    sensitive: bool
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class DraftPage(BaseModel):
    items: list[DraftOut]
    next_cursor: str | None


class CitationOut(BaseModel):
    chunk_id: uuid.UUID
    source_id: uuid.UUID
    source_name: str


class DraftVersionOut(_Out):
    version: int
    body_text: str
    author_type: Literal["ai", "user"]
    author_id: str
    ai_generated: bool
    model: str | None
    provider: str | None
    prompt_version: int | None
    citations: list[CitationOut]
    quality: dict[str, Any]
    created_at: datetime


class DraftActionOut(_Out):
    action: str
    version: int | None
    actor_type: str
    actor_id: str
    reason: str | None
    created_at: datetime


class SendCommandOut(_Out):
    id: uuid.UUID
    version: int
    status: str
    approved_by: str
    approved_at: datetime
    to_address: str
    attempts: int
    last_error: str | None
    outbound_message_id: str | None
    sent_at: datetime | None


class DraftDetail(DraftOut):
    # The AI summary is model output about untrusted content; it is display text only.
    summary: str
    original_message: MessageOut
    thread_messages: list[MessageOut]
    versions: list[DraftVersionOut]
    actions: list[DraftActionOut]
    send_commands: list[SendCommandOut]


class DraftEdit(_In):
    text: str = Field(min_length=1, max_length=20_000)
    expected_version: int = Field(ge=0)


class RejectIn(_In):
    reason: Literal["inaccurate", "inappropriate", "not_needed", "handled_elsewhere", "other"] = "other"


class AssignIn(_In):
    assignee: str | None = Field(default=None, max_length=128)


class EscalateIn(_In):
    reason: Literal["complaint", "legal", "privacy_request", "unsubscribe", "needs_expert", "suspicious", "other"]


class VersionIn(_In):
    """Approve and send take ONLY the version. Recipients and content come from persisted state."""

    version: int = Field(ge=1)


class ApprovalOut(BaseModel):
    draft_id: uuid.UUID
    version: int
    status: str
    send_command_id: uuid.UUID

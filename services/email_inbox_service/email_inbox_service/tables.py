"""SQLAlchemy Core tables owned by Email Inbox & AI Draft Service. Only this service touches them (build.md §3.2).

Platform-scoped (ids only): mailbox_directory (envelope recipient hash -> tenant/mailbox), provider_events
(SNS dedupe + inbound processing queue). Tenant-owned (forced RLS): mailboxes, threads, messages,
attachments, drafts, send_commands. Append-only history: draft_versions, draft_actions.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from nexus_core.tables import idempotency_table, inbox_table, outbox_table

metadata = MetaData()

outbox = outbox_table(metadata)
inbox = inbox_table(metadata)
idempotency_keys = idempotency_table(metadata)

DRAFT_STATUSES = (
    "queued",
    "generating",
    "pending_review",
    "approved",
    "sending",
    "sent",
    "send_failed",
    "rejected",
    "generation_failed",
)
SEND_STATUSES = ("queued", "sending", "sent", "failed", "blocked", "canceled")
SCAN_STATUSES = ("clean", "infected", "skipped", "error", "rejected", "purged")
DRAFT_ACTIONS = (
    "requested",
    "generated",
    "generation_failed",
    "edited",
    "regenerated",
    "approved",
    "rejected",
    "assigned",
    "escalated",
    "send_retry",
    "sent",
    "send_failed",
    "send_blocked",
    "send_canceled",
)


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _uuid(name: str, nullable: bool = False, **kw: object) -> Column:  # type: ignore[type-arg]
    return Column(name, UUID(as_uuid=True), nullable=nullable, **kw)  # type: ignore[arg-type]


def _pk() -> Column:  # type: ignore[type-arg]
    return Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


def _ts() -> list[Column]:  # type: ignore[type-arg]
    return [
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ]


# ------------------------------------------------------------------------------------------- platform-scoped
mailbox_directory = Table(
    "mailbox_directory",
    metadata,
    Column("address_hash", String(64), primary_key=True),  # sha256(normalized envelope address); no PII
    _uuid("tenant_id"),
    _uuid("mailbox_id", unique=True),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("status IN ('active','disabled')", name="ck_mailbox_directory_status"),
    comment="platform_scoped: ids-only map of envelope recipient hash to tenant and mailbox",
)

provider_events = Table(
    "provider_events",
    metadata,
    _pk(),
    Column("provider", String(16), nullable=False),
    Column("event_id", String(128), nullable=False),  # SNS MessageId
    _uuid("tenant_id"),
    _uuid("mailbox_id"),
    Column("bucket", String(63), nullable=False),
    Column("object_key", String(512), nullable=False),
    Column("ses_message_id", String(128), nullable=True),
    Column("verdicts", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("status", String(16), nullable=False, server_default="queued"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", String(64), nullable=True),
    _uuid("message_id", nullable=True),
    *_ts(),
    UniqueConstraint("provider", "event_id", "mailbox_id", name="uq_provider_events_event"),
    CheckConstraint(
        "status IN ('queued','processing','processed','duplicate','failed')", name="ck_provider_events_status"
    ),
    Index("ix_provider_events_status", "status", "updated_at"),
    comment="platform_scoped: verified SNS notification dedupe and inbound work queue (ids and object keys only)",
)

# ------------------------------------------------------------------------------------------- tenant-owned
mailboxes = Table(
    "mailboxes",
    metadata,
    _pk(),
    _uuid("tenant_id"),
    Column("address", String(320), nullable=False),
    Column("display_name", String(200), nullable=False, server_default=""),
    _uuid("agent_id", nullable=True),
    Column("auto_draft", Boolean, nullable=False, server_default=text("true")),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_mailboxes_tenant_id"),
    UniqueConstraint("tenant_id", "address", name="uq_mailboxes_address"),
    CheckConstraint("status IN ('active','disabled')", name="ck_mailboxes_status"),
)

threads = Table(
    "threads",
    metadata,
    _pk(),
    _uuid("tenant_id"),
    _uuid("mailbox_id"),
    Column("subject", String(998), nullable=False, server_default=""),
    _uuid("contact_id", nullable=True),
    _uuid("campaign_id", nullable=True),
    Column("status", String(16), nullable=False, server_default="open"),
    Column("priority", String(16), nullable=False, server_default="normal"),
    Column("intent", String(32), nullable=True),
    Column("assigned_to", String(128), nullable=True),
    Column("message_count", Integer, nullable=False, server_default="0"),
    Column("last_message_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_threads_tenant_id"),
    ForeignKeyConstraint(
        ["tenant_id", "mailbox_id"], ["mailboxes.tenant_id", "mailboxes.id"], name="fk_threads_mailbox"
    ),
    CheckConstraint("status IN ('open','awaiting_review','replied','closed')", name="ck_threads_status"),
    CheckConstraint("priority IN ('low','normal','high','urgent')", name="ck_threads_priority"),
    Index("ix_threads_recent", "tenant_id", "last_message_at"),
)

messages = Table(
    "messages",
    metadata,
    _pk(),
    _uuid("tenant_id"),
    _uuid("thread_id"),
    _uuid("mailbox_id"),
    Column("direction", String(8), nullable=False),
    Column("rfc_message_id", String(998), nullable=True),
    Column("in_reply_to", String(998), nullable=True),
    Column("rfc_references", ARRAY(Text), nullable=False, server_default=text("'{}'")),
    Column("from_address", String(320), nullable=False),
    Column("from_name", String(200), nullable=False, server_default=""),
    Column("to_addresses", ARRAY(String(320)), nullable=False, server_default=text("'{}'")),
    Column("cc_addresses", ARRAY(String(320)), nullable=False, server_default=text("'{}'")),
    Column("subject", String(998), nullable=False, server_default=""),
    Column("text_body", Text, nullable=False, server_default=""),
    Column("html_sanitized", Text, nullable=True),
    Column("raw_object_key", String(512), nullable=True),
    Column("sns_message_id", String(128), nullable=True),
    Column("size_bytes", BigInteger, nullable=False, server_default="0"),
    Column("auto_flags", ARRAY(String(32)), nullable=False, server_default=text("'{}'")),
    _uuid("contact_id", nullable=True),
    _uuid("campaign_id", nullable=True),
    _uuid("draft_id", nullable=True),
    Column("draft_version", Integer, nullable=True),
    Column("outbound_message_id", String(128), nullable=True),
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("content_purged_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_messages_tenant_id"),
    UniqueConstraint("tenant_id", "rfc_message_id", name="uq_messages_rfc_message_id"),
    ForeignKeyConstraint(["tenant_id", "thread_id"], ["threads.tenant_id", "threads.id"], name="fk_messages_thread"),
    ForeignKeyConstraint(
        ["tenant_id", "mailbox_id"], ["mailboxes.tenant_id", "mailboxes.id"], name="fk_messages_mailbox"
    ),
    CheckConstraint("direction IN ('inbound','outbound')", name="ck_messages_direction"),
    Index("ix_messages_thread", "tenant_id", "thread_id", "received_at"),
)

attachments = Table(
    "attachments",
    metadata,
    _pk(),
    _uuid("tenant_id"),
    _uuid("message_id"),
    Column("filename", String(255), nullable=False),
    Column("content_type", String(127), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("object_key", String(512), nullable=True),
    Column("scan_status", String(16), nullable=False),
    Column("scan_signature", String(200), nullable=True),
    Column("quarantined", Boolean, nullable=False, server_default=text("false")),
    Column("rejection_reason", String(64), nullable=True),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_attachments_tenant_id"),
    ForeignKeyConstraint(
        ["tenant_id", "message_id"], ["messages.tenant_id", "messages.id"], name="fk_attachments_message"
    ),
    CheckConstraint(f"scan_status IN ({_in(SCAN_STATUSES)})", name="ck_attachments_scan_status"),
    # An infected file can never be marked downloadable.
    CheckConstraint("NOT (quarantined AND scan_status = 'clean')", name="ck_attachments_quarantine"),
)

drafts = Table(
    "drafts",
    metadata,
    _pk(),
    _uuid("tenant_id"),
    _uuid("thread_id"),
    _uuid("message_id"),  # the inbound message this draft replies to
    _uuid("mailbox_id"),
    Column("status", String(20), nullable=False, server_default="queued"),
    Column("current_version", Integer, nullable=False, server_default="0"),
    Column("approved_version", Integer, nullable=True),
    Column("approved_by", String(128), nullable=True),
    Column("approved_at", DateTime(timezone=True), nullable=True),
    Column("assigned_to", String(128), nullable=True),
    Column("escalated", Boolean, nullable=False, server_default=text("false")),
    Column("escalation_reason", String(64), nullable=True),
    Column("intent", String(32), nullable=True),
    Column("priority", String(16), nullable=False, server_default="normal"),
    Column("sensitive", Boolean, nullable=False, server_default=text("false")),
    Column("summary", Text, nullable=False, server_default=""),
    Column("generation_attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", String(64), nullable=True),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_drafts_tenant_id"),
    UniqueConstraint("tenant_id", "message_id", name="uq_drafts_message"),
    ForeignKeyConstraint(["tenant_id", "thread_id"], ["threads.tenant_id", "threads.id"], name="fk_drafts_thread"),
    ForeignKeyConstraint(["tenant_id", "message_id"], ["messages.tenant_id", "messages.id"], name="fk_drafts_message"),
    CheckConstraint(f"status IN ({_in(DRAFT_STATUSES)})", name="ck_drafts_status"),
    CheckConstraint(
        "status NOT IN ('approved','sending','sent','send_failed') OR "
        "(approved_version IS NOT NULL AND approved_by IS NOT NULL)",
        name="ck_drafts_approval_recorded",
    ),
    Index("ix_drafts_queue", "tenant_id", "status", "created_at"),
)

draft_versions = Table(
    "draft_versions",
    metadata,
    _pk(),
    _uuid("tenant_id"),
    _uuid("draft_id"),
    Column("version", Integer, nullable=False),
    Column("body_text", Text, nullable=False),
    Column("author_type", String(8), nullable=False),
    Column("author_id", String(128), nullable=False),
    Column("model", String(100), nullable=True),
    Column("provider", String(32), nullable=True),
    Column("prompt_version", Integer, nullable=True),
    _uuid("run_id", nullable=True),
    Column("citations", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("quality", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_draft_versions_tenant_id"),
    UniqueConstraint("tenant_id", "draft_id", "version", name="uq_draft_versions_version"),
    ForeignKeyConstraint(["tenant_id", "draft_id"], ["drafts.tenant_id", "drafts.id"], name="fk_draft_versions_draft"),
    CheckConstraint("author_type IN ('ai','user')", name="ck_draft_versions_author"),
    CheckConstraint("version >= 1", name="ck_draft_versions_version"),
    comment="append_only: immutable draft content history with AI provenance",
)

draft_actions = Table(
    "draft_actions",
    metadata,
    _pk(),
    _uuid("tenant_id"),
    _uuid("draft_id"),
    Column("action", String(20), nullable=False),
    Column("version", Integer, nullable=True),
    Column("actor_type", String(16), nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("reason", String(64), nullable=True),
    Column("details", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_draft_actions_tenant_id"),
    ForeignKeyConstraint(["tenant_id", "draft_id"], ["drafts.tenant_id", "drafts.id"], name="fk_draft_actions_draft"),
    CheckConstraint(f"action IN ({_in(DRAFT_ACTIONS)})", name="ck_draft_actions_action"),
    CheckConstraint("actor_type IN ('user','service','system')", name="ck_draft_actions_actor"),
    Index("ix_draft_actions_draft", "tenant_id", "draft_id", "created_at"),
    comment="append_only: immutable review and approval history",
)

send_commands = Table(
    "send_commands",
    metadata,
    _pk(),
    _uuid("tenant_id"),
    _uuid("draft_id"),
    Column("version", Integer, nullable=False),
    Column("status", String(16), nullable=False, server_default="queued"),
    Column("approved_by", String(128), nullable=False),
    Column("approved_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Frozen at approval: exactly what was approved is what gets sent.
    Column("from_address", String(320), nullable=False),
    Column("to_address", String(320), nullable=False),
    Column("subject", String(998), nullable=False),
    Column("in_reply_to", String(998), nullable=True),
    Column("rfc_references", ARRAY(Text), nullable=False, server_default=text("'{}'")),
    Column("idempotency_key", String(128), nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", String(64), nullable=True),
    Column("outbound_message_id", String(128), nullable=True),
    Column("provider_state", String(32), nullable=True),
    Column("sent_at", DateTime(timezone=True), nullable=True),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_send_commands_tenant_id"),
    UniqueConstraint("tenant_id", "draft_id", "version", name="uq_send_commands_draft_version"),
    ForeignKeyConstraint(["tenant_id", "draft_id"], ["drafts.tenant_id", "drafts.id"], name="fk_send_commands_draft"),
    ForeignKeyConstraint(
        ["tenant_id", "draft_id", "version"],
        ["draft_versions.tenant_id", "draft_versions.draft_id", "draft_versions.version"],
        name="fk_send_commands_version",
    ),
    CheckConstraint(f"status IN ({_in(SEND_STATUSES)})", name="ck_send_commands_status"),
    Index("ix_send_commands_status", "tenant_id", "status", "updated_at"),
)

PLATFORM_SCOPED = {
    "mailbox_directory": "ids-only map of envelope recipient hash to tenant and mailbox",
    "provider_events": "verified SNS notification dedupe and inbound work queue (ids and object keys only)",
}
TENANT_TABLES = ("mailboxes", "threads", "messages", "attachments", "drafts", "send_commands")
APPEND_ONLY_TABLES = ("draft_versions", "draft_actions")

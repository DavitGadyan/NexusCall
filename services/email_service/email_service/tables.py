"""SQLAlchemy Core tables owned by email-service. Only this service touches them (build.md §3.2).

Tenant-owned (forced RLS): sending_domains, sender_identities, templates, template_versions (append-only),
email_campaigns, email_messages, provider_events (append-only), unsubscribe_tokens, usage_counters.

Platform-scoped, ids-only lookup tables (no PII): message_directory, unsubscribe_token_directory,
campaign_schedule_index, tenant_activity. They let unauthenticated/provider paths (SES webhooks, one-click
unsubscribe) and periodic jobs find the owning tenant from data this service wrote itself, never from the
payload.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
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


def _ts() -> list[Column]:  # type: ignore[type-arg]
    return [
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ]


def _id() -> Column:  # type: ignore[type-arg]
    return Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


def _tenant() -> Column:  # type: ignore[type-arg]
    return Column("tenant_id", UUID(as_uuid=True), nullable=False)


CAMPAIGN_STATES = ("draft", "scheduled", "running", "paused", "completed", "canceled", "failed")
MESSAGE_STATES = (
    "pending",
    "queued",
    "sending",
    "accepted",
    "delivered",
    "bounced",
    "complained",
    "rejected",
    "unsubscribed",
    "failed",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


# ---------------------------------------------------------------------------------------------- tenant-owned
sending_domains = Table(
    "sending_domains",
    metadata,
    _id(),
    _tenant(),
    Column("domain", String(253), nullable=False),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("dkim_tokens", ARRAY(String(64)), nullable=False, server_default=text("'{}'")),
    Column("dkim_status", String(16), nullable=False, server_default="pending"),
    Column("spf_status", String(16), nullable=False, server_default="pending"),
    Column("dmarc_status", String(16), nullable=False, server_default="pending"),
    Column("last_checked_at", DateTime(timezone=True), nullable=True),
    Column("verified_at", DateTime(timezone=True), nullable=True),
    Column("created_by", String(160), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_sending_domains_tenant_id"),
    UniqueConstraint("tenant_id", "domain", name="uq_sending_domains_domain"),
    CheckConstraint(_in("status", ("pending", "verified", "failed", "disabled")), name="ck_sending_domains_status"),
    CheckConstraint(_in("dkim_status", ("pending", "pass", "fail")), name="ck_sending_domains_dkim"),
    CheckConstraint(_in("spf_status", ("pending", "pass", "fail")), name="ck_sending_domains_spf"),
    CheckConstraint(_in("dmarc_status", ("pending", "pass", "fail")), name="ck_sending_domains_dmarc"),
)

sender_identities = Table(
    "sender_identities",
    metadata,
    _id(),
    _tenant(),
    Column("domain_id", UUID(as_uuid=True), nullable=False),
    Column("from_name", String(120), nullable=False),
    Column("from_address", String(320), nullable=False),
    Column("reply_to", String(320), nullable=True),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("created_by", String(160), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_sender_identities_tenant_id"),
    UniqueConstraint("tenant_id", "from_address", name="uq_sender_identities_address"),
    ForeignKeyConstraint(
        ["tenant_id", "domain_id"], ["sending_domains.tenant_id", "sending_domains.id"], name="fk_senders_domain"
    ),
    CheckConstraint(_in("status", ("active", "disabled")), name="ck_sender_identities_status"),
)

templates = Table(
    "templates",
    metadata,
    _id(),
    _tenant(),
    Column("name", String(120), nullable=False),
    Column("category", String(16), nullable=False),
    Column("current_version", Integer, nullable=False, server_default="1"),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("created_by", String(160), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_templates_tenant_id"),
    UniqueConstraint("tenant_id", "name", name="uq_templates_name"),
    CheckConstraint(_in("category", ("campaign", "transactional")), name="ck_templates_category"),
    CheckConstraint(_in("status", ("active", "archived")), name="ck_templates_status"),
)

template_versions = Table(
    "template_versions",
    metadata,
    _id(),
    _tenant(),
    Column("template_id", UUID(as_uuid=True), nullable=False),
    Column("version", Integer, nullable=False),
    Column("subject", String(998), nullable=False),
    Column("html", Text, nullable=False),  # sanitized with bleach before insert
    Column("text", Text, nullable=False),
    Column("variables", ARRAY(String(32)), nullable=False, server_default=text("'{}'")),
    Column("created_by", String(160), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_template_versions_tenant_id"),
    UniqueConstraint("tenant_id", "template_id", "version", name="uq_template_versions_version"),
    ForeignKeyConstraint(
        ["tenant_id", "template_id"], ["templates.tenant_id", "templates.id"], name="fk_template_versions_template"
    ),
    comment="append_only: immutable template version history (campaigns pin a version)",
)

email_campaigns = Table(
    "email_campaigns",
    metadata,
    _id(),
    _tenant(),
    Column("name", String(200), nullable=False),
    Column("sender_id", UUID(as_uuid=True), nullable=False),
    Column("template_id", UUID(as_uuid=True), nullable=False),
    Column("template_version", Integer, nullable=True),  # pinned at launch
    Column("segment_id", UUID(as_uuid=True), nullable=False),
    Column("state", String(16), nullable=False, server_default="draft"),
    Column("timezone", String(64), nullable=False, server_default="UTC"),
    Column("max_per_minute", Integer, nullable=False, server_default="60"),
    Column("scheduled_at", DateTime(timezone=True), nullable=True),
    Column("ends_at", DateTime(timezone=True), nullable=True),
    Column("launched_at", DateTime(timezone=True), nullable=True),
    Column("launched_by", String(160), nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("pause_reason", String(32), nullable=True),
    Column("snapshot_cursor", String(128), nullable=True),
    Column("snapshot_done", Boolean, nullable=False, server_default=text("false")),
    Column("last_error", String(64), nullable=True),
    Column("created_by", String(160), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_email_campaigns_tenant_id"),
    ForeignKeyConstraint(
        ["tenant_id", "sender_id"], ["sender_identities.tenant_id", "sender_identities.id"], name="fk_campaign_sender"
    ),
    ForeignKeyConstraint(
        ["tenant_id", "template_id"], ["templates.tenant_id", "templates.id"], name="fk_campaign_template"
    ),
    CheckConstraint(_in("state", CAMPAIGN_STATES), name="ck_email_campaigns_state"),
    CheckConstraint("max_per_minute BETWEEN 1 AND 10000", name="ck_email_campaigns_rate"),
    Index("ix_email_campaigns_state", "tenant_id", "state"),
)

email_messages = Table(
    "email_messages",
    metadata,
    _id(),
    _tenant(),
    Column("category", String(16), nullable=False),
    Column("state", String(16), nullable=False, server_default="pending"),
    Column("campaign_id", UUID(as_uuid=True), nullable=True),
    Column("contact_id", UUID(as_uuid=True), nullable=True),
    Column("sender_id", UUID(as_uuid=True), nullable=True),
    Column("template_id", UUID(as_uuid=True), nullable=True),
    Column("template_version", Integer, nullable=True),
    Column("from_address", String(320), nullable=False),
    Column("from_name", String(120), nullable=True),
    Column("reply_to", String(320), nullable=True),
    Column("to_address", String(320), nullable=True),  # resolved from crm when a campaign row is queued
    Column("to_hash", String(64), nullable=True),
    Column("subject", Text, nullable=True),
    Column("body_text", Text, nullable=True),
    Column("body_html", Text, nullable=True),
    Column("extra_headers", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("render_vars", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("rfc_message_id", String(255), nullable=False),
    Column("idempotency_key", String(255), nullable=False),
    Column("provider", String(16), nullable=False, server_default="ses"),
    Column("provider_message_id", String(255), nullable=True),
    Column("source_type", String(32), nullable=True),
    Column("source_id", String(128), nullable=True),
    Column("source_version", Integer, nullable=True),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("next_attempt_at", DateTime(timezone=True), nullable=True),
    Column("claimed_at", DateTime(timezone=True), nullable=True),
    Column("ambiguous_at", DateTime(timezone=True), nullable=True),
    Column("last_error", String(64), nullable=True),
    Column("suppression_reason", String(16), nullable=True),
    Column("suppression_synced_at", DateTime(timezone=True), nullable=True),
    Column("queued_at", DateTime(timezone=True), nullable=True),
    Column("accepted_at", DateTime(timezone=True), nullable=True),
    Column("delivered_at", DateTime(timezone=True), nullable=True),
    Column("state_changed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_email_messages_tenant_id"),
    UniqueConstraint("tenant_id", "idempotency_key", name="uq_email_messages_idempotency"),
    UniqueConstraint("tenant_id", "rfc_message_id", name="uq_email_messages_rfc_id"),
    ForeignKeyConstraint(
        ["tenant_id", "campaign_id"], ["email_campaigns.tenant_id", "email_campaigns.id"], name="fk_messages_campaign"
    ),
    ForeignKeyConstraint(
        ["tenant_id", "sender_id"], ["sender_identities.tenant_id", "sender_identities.id"], name="fk_messages_sender"
    ),
    CheckConstraint(_in("category", ("campaign", "transactional", "reply")), name="ck_email_messages_category"),
    CheckConstraint(_in("state", MESSAGE_STATES), name="ck_email_messages_state"),
    CheckConstraint(
        "suppression_reason IS NULL OR " + _in("suppression_reason", ("bounce", "complaint", "opt_out")),
        name="ck_email_messages_suppression",
    ),
    CheckConstraint("campaign_id IS NULL OR contact_id IS NOT NULL", name="ck_email_messages_campaign_contact"),
    Index(
        "uq_email_messages_campaign_contact",
        "tenant_id",
        "campaign_id",
        "contact_id",
        unique=True,
        postgresql_where=text("campaign_id IS NOT NULL"),
    ),
    Index("ix_email_messages_campaign_state", "tenant_id", "campaign_id", "state"),
    Index("ix_email_messages_state_due", "tenant_id", "state", "next_attempt_at"),
    Index("ix_email_messages_to_hash", "tenant_id", "to_hash"),
    Index(
        "ix_email_messages_suppression_pending",
        "tenant_id",
        postgresql_where=text("suppression_reason IS NOT NULL AND suppression_synced_at IS NULL"),
    ),
)

provider_events = Table(
    "provider_events",
    metadata,
    _id(),
    _tenant(),
    Column("provider", String(16), nullable=False),
    Column("provider_event_id", String(128), nullable=False),
    Column("event_type", String(32), nullable=False),
    Column("message_id", UUID(as_uuid=True), nullable=True),
    Column("outcome", String(16), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_provider_events_tenant_id"),
    UniqueConstraint("tenant_id", "provider", "provider_event_id", name="uq_provider_events_event"),
    comment="append_only: provider webhook ledger (dedupe on provider event id)",
)

unsubscribe_tokens = Table(
    "unsubscribe_tokens",
    metadata,
    _id(),
    _tenant(),
    Column("token_hash", String(64), nullable=False),  # sha256 of the raw token; the raw token is never stored
    Column("message_id", UUID(as_uuid=True), nullable=False),
    Column("campaign_id", UUID(as_uuid=True), nullable=True),
    Column("contact_id", UUID(as_uuid=True), nullable=True),
    Column("address_hash", String(64), nullable=False),
    Column("used_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_unsubscribe_tokens_tenant_id"),
    UniqueConstraint("tenant_id", "token_hash", name="uq_unsubscribe_tokens_hash"),
    ForeignKeyConstraint(
        ["tenant_id", "message_id"], ["email_messages.tenant_id", "email_messages.id"], name="fk_unsubscribe_message"
    ),
    Index("ix_unsubscribe_tokens_address", "tenant_id", "address_hash"),
)

usage_counters = Table(
    "usage_counters",
    metadata,
    _tenant(),
    Column("day", Date, nullable=False),
    Column("sent", Integer, nullable=False, server_default="0"),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("tenant_id", "day"),
)

# ---------------------------------------------------------------------------------------------- platform-scoped
message_directory = Table(
    "message_directory",
    metadata,
    Column("message_id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("provider_message_id", String(255), nullable=True, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

unsubscribe_token_directory = Table(
    "unsubscribe_token_directory",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

campaign_schedule_index = Table(
    "campaign_schedule_index",
    metadata,
    Column("campaign_id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

tenant_activity = Table(
    "tenant_activity",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), primary_key=True),
    Column("last_activity_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

PLATFORM_SCOPED = {
    "message_directory": "ids-only message -> tenant index for provider webhooks",
    "unsubscribe_token_directory": "token-hash -> tenant lookup for public one-click unsubscribe (no PII)",
    "campaign_schedule_index": "ids-only index of live campaigns for the scheduler",
    "tenant_activity": "ids-only index of tenants with recent email activity for sweepers",
}
TENANT_TABLES = (
    "sending_domains",
    "sender_identities",
    "templates",
    "email_campaigns",
    "email_messages",
    "unsubscribe_tokens",
    "usage_counters",
)
APPEND_ONLY_TABLES = ("template_versions", "provider_events")

for _name, _reason in PLATFORM_SCOPED.items():
    metadata.tables[_name].comment = f"platform_scoped: {_reason}"

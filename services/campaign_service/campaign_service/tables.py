"""SQLAlchemy Core tables owned by Campaign Service. Only this service touches them (build.md §3.2, §7).

Tenant-owned (forced RLS): call_campaigns, campaign_contacts, kill_switches, campaign_events (append-only).
Platform-scoped: dispatch_registry (ids-only index of live campaigns so the dispatcher can find work
without a tenant context; every unit of work then runs inside that tenant's RLS session).
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
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

CAMPAIGN_STATES = ("draft", "scheduled", "running", "paused", "completed", "canceled", "failed")
CONTACT_STATES = ("pending", "in_flight", "completed", "failed", "skipped", "retry_wait")


def _ts() -> list[Column]:  # type: ignore[type-arg]
    return [
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ]


call_campaigns = Table(
    "call_campaigns",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("name", String(200), nullable=False),
    Column("description", String(1000), nullable=True),
    Column("agent_id", UUID(as_uuid=True), nullable=True),
    Column("segment_id", UUID(as_uuid=True), nullable=True),
    Column("state", String(16), nullable=False, server_default="draft"),
    Column("timezone", String(64), nullable=False, server_default="UTC"),
    Column("window_start", String(5), nullable=False, server_default="09:00"),
    Column("window_end", String(5), nullable=False, server_default="20:00"),
    Column(
        "window_days",
        ARRAY(String(3)),
        nullable=False,
        server_default=text("'{mon,tue,wed,thu,fri}'"),
    ),
    Column("max_concurrency", Integer, nullable=False, server_default="2"),
    Column("calls_per_minute", Integer, nullable=False, server_default="10"),
    Column("max_attempts", Integer, nullable=False, server_default="3"),
    Column("retry_delay_seconds", Integer, nullable=False, server_default="3600"),
    Column("start_at", DateTime(timezone=True), nullable=True),
    Column("end_at", DateTime(timezone=True), nullable=True),
    # Audience snapshot progress (calls.schedule task).
    Column("snapshot_status", String(16), nullable=False, server_default="none"),
    Column("snapshot_cursor", String(512), nullable=True),
    Column("snapshot_total", Integer, nullable=False, server_default="0"),
    Column("snapshot_updated_at", DateTime(timezone=True), nullable=True),
    # Calls-per-minute token bucket, persisted so pacing survives restarts and replica changes.
    Column("pacing_tokens", Float, nullable=True),
    Column("pacing_refilled_at", DateTime(timezone=True), nullable=True),
    Column("failure_reason", String(64), nullable=True),
    Column("launched_at", DateTime(timezone=True), nullable=True),
    Column("launched_by", String(140), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("created_by", String(140), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_call_campaigns_tenant_id"),
    CheckConstraint(
        "state IN ('draft','scheduled','running','paused','completed','canceled','failed')",
        name="ck_call_campaigns_state",
    ),
    CheckConstraint("snapshot_status IN ('none','pending','running','completed','failed')", name="ck_snapshot_status"),
    CheckConstraint("window_start ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'", name="ck_window_start"),
    CheckConstraint("window_end ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'", name="ck_window_end"),
    CheckConstraint("window_start < window_end", name="ck_window_order"),
    CheckConstraint("max_concurrency BETWEEN 1 AND 1000", name="ck_max_concurrency"),
    CheckConstraint("calls_per_minute BETWEEN 1 AND 600", name="ck_calls_per_minute"),
    CheckConstraint("max_attempts BETWEEN 1 AND 10", name="ck_max_attempts"),
    CheckConstraint("retry_delay_seconds BETWEEN 60 AND 604800", name="ck_retry_delay"),
    CheckConstraint("end_at IS NULL OR start_at IS NULL OR start_at < end_at", name="ck_campaign_period"),
    Index("ix_call_campaigns_created", "tenant_id", "created_at", "id"),
)

campaign_contacts = Table(
    "campaign_contacts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("campaign_id", UUID(as_uuid=True), nullable=False),
    Column("contact_id", UUID(as_uuid=True), nullable=False),
    Column("state", String(16), nullable=False, server_default="pending"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("next_attempt_at", DateTime(timezone=True), nullable=True),
    Column("last_call_id", UUID(as_uuid=True), nullable=True),
    Column("outcome", String(64), nullable=True),
    Column("skip_reason", String(64), nullable=True),
    Column("dispatched_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_campaign_contacts_tenant_id"),
    UniqueConstraint("tenant_id", "campaign_id", "contact_id", name="uq_campaign_contacts_member"),
    ForeignKeyConstraint(
        ["tenant_id", "campaign_id"],
        ["call_campaigns.tenant_id", "call_campaigns.id"],
        ondelete="CASCADE",
        name="fk_campaign_contacts_campaign",
    ),
    CheckConstraint(
        "state IN ('pending','in_flight','completed','failed','skipped','retry_wait')",
        name="ck_campaign_contacts_state",
    ),
    CheckConstraint("attempts >= 0", name="ck_campaign_contacts_attempts"),
    Index("ix_campaign_contacts_due", "tenant_id", "campaign_id", "state", "next_attempt_at"),
    Index("ix_campaign_contacts_call", "tenant_id", "last_call_id"),
)

campaign_events = Table(
    "campaign_events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("campaign_id", UUID(as_uuid=True), nullable=False),
    Column("kind", String(48), nullable=False),
    Column("from_state", String(16), nullable=True),
    Column("to_state", String(16), nullable=True),
    Column("contact_id", UUID(as_uuid=True), nullable=True),
    Column("call_id", UUID(as_uuid=True), nullable=True),
    Column("actor", String(140), nullable=False),
    Column("data", JSONB, nullable=False, server_default=text("'{}'::jsonb")),  # ids and enums only
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_campaign_events_tenant_id"),
    ForeignKeyConstraint(
        ["tenant_id", "campaign_id"],
        ["call_campaigns.tenant_id", "call_campaigns.id"],
        name="fk_campaign_events_campaign",
    ),
    Index("ix_campaign_events_campaign", "tenant_id", "campaign_id", "created_at"),
    Index("ix_campaign_events_kind", "tenant_id", "kind", "created_at"),
    comment="append_only: campaign state transitions and dispatch history",
)

kill_switches = Table(
    "kill_switches",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("scope", String(16), nullable=False, server_default="tenant"),
    Column("channel", String(8), nullable=False, server_default="call"),
    Column("engaged", Boolean, nullable=False, server_default=text("false")),
    Column("reason", String(300), nullable=True),
    Column("changed_by", String(140), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_kill_switches_tenant_id"),
    UniqueConstraint("tenant_id", "scope", "channel", name="uq_kill_switches_scope"),
    CheckConstraint("scope IN ('tenant')", name="ck_kill_switches_scope"),
    CheckConstraint("channel IN ('call')", name="ck_kill_switches_channel"),
)

dispatch_registry = Table(
    "dispatch_registry",
    metadata,
    Column("campaign_id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("state", String(16), nullable=False),
    Column("snapshot_status", String(16), nullable=False, server_default="none"),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("state IN ('running','paused')", name="ck_dispatch_registry_state"),
    Index("ix_dispatch_registry_state", "state"),
)

PLATFORM_SCOPED = {"dispatch_registry": "ids-only index of running/paused campaigns for the dispatcher"}
TENANT_TABLES = ("call_campaigns", "campaign_contacts", "kill_switches")
APPEND_ONLY_TABLES = ("campaign_events",)

for _name, _reason in PLATFORM_SCOPED.items():
    metadata.tables[_name].comment = f"platform_scoped: {_reason}"

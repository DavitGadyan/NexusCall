"""SQLAlchemy Core tables owned by the Call Orchestration Service. Only this service touches them (build.md §3.2).

Tenant-owned (forced RLS): calls, call_attempts, provider_events, inbound_numbers, call_outcomes.
Platform-scoped, ids only (needed before a tenant is known, or by sweepers that span tenants):
number_directory (E.164 -> tenant, for inbound webhooks) and call_directory (call / provider call id -> tenant,
for status webhooks and the sweeper / reconciliation jobs).
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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from nexus_core.tables import idempotency_table, inbox_table, outbox_table

metadata = MetaData()

outbox = outbox_table(metadata)
inbox = inbox_table(metadata)
idempotency_keys = idempotency_table(metadata)

CALL_STATES = (
    "queued",
    "scheduled",
    "initiating",
    "ringing",
    "connected",
    "in_progress",
    "completed",
    "failed",
    "canceled",
    "opted_out",
)
_STATES_SQL = ",".join(f"'{s}'" for s in CALL_STATES)


def _ts() -> list[Column]:  # type: ignore[type-arg]
    return [
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ]


calls = Table(
    "calls",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("direction", String(8), nullable=False),
    Column("state", String(16), nullable=False),
    Column("contact_id", UUID(as_uuid=True), nullable=True),
    Column("agent_id", UUID(as_uuid=True), nullable=True),
    Column("campaign_id", UUID(as_uuid=True), nullable=True),
    Column("campaign_contact_id", UUID(as_uuid=True), nullable=True),
    Column("inbound_number_id", UUID(as_uuid=True), nullable=True),
    Column("from_e164", String(16), nullable=True),
    Column("to_e164", String(16), nullable=True),
    Column("provider", String(16), nullable=False, server_default="twilio"),
    Column("provider_call_id", String(64), nullable=True),
    Column("is_test", Boolean, nullable=False, server_default=text("false")),
    Column("scheduled_for", DateTime(timezone=True), nullable=True),
    Column("attempt_count", Integer, nullable=False, server_default="0"),
    Column("initiated_at", DateTime(timezone=True), nullable=True),
    Column("answered_at", DateTime(timezone=True), nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("ended_at", DateTime(timezone=True), nullable=True),
    Column("duration_seconds", Integer, nullable=True),
    Column("end_reason", String(64), nullable=True),
    Column("blocked_reason", String(64), nullable=True),
    Column("last_provider_status", String(16), nullable=True),
    Column("last_provider_sequence", Integer, nullable=True),
    Column("handoff_state", String(16), nullable=True),
    Column("cost_micros", BigInteger, nullable=False, server_default="0"),
    Column("opted_out", Boolean, nullable=False, server_default=text("false")),
    Column("created_by", String(160), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_calls_tenant_id"),
    UniqueConstraint("provider_call_id", name="uq_calls_provider_call_id"),
    CheckConstraint("direction IN ('inbound','outbound')", name="ck_calls_direction"),
    CheckConstraint(f"state IN ({_STATES_SQL})", name="ck_calls_state"),
    CheckConstraint(
        "handoff_state IS NULL OR handoff_state IN ('transferring','unavailable','failed')", name="ck_calls_handoff"
    ),
    CheckConstraint("cost_micros >= 0", name="ck_calls_cost"),
    Index("ix_calls_tenant_created", "tenant_id", "created_at"),
    Index("ix_calls_tenant_state", "tenant_id", "state"),
    Index("ix_calls_campaign", "tenant_id", "campaign_id"),
)

call_attempts = Table(
    "call_attempts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("call_id", UUID(as_uuid=True), nullable=False),
    Column("attempt_no", Integer, nullable=False),
    # Sent to the provider on create and used to find the call again after an ambiguous result.
    Column("idempotency_token", String(80), nullable=False),
    Column("status", String(16), nullable=False),
    Column("provider_call_id", String(64), nullable=True),
    Column("error_code", String(64), nullable=True),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_call_attempts_tenant_id"),
    UniqueConstraint("tenant_id", "call_id", "attempt_no", name="uq_call_attempts_no"),
    UniqueConstraint("idempotency_token", name="uq_call_attempts_token"),
    ForeignKeyConstraint(
        ["tenant_id", "call_id"], ["calls.tenant_id", "calls.id"], ondelete="CASCADE", name="fk_call_attempts_call"
    ),
    CheckConstraint(
        "status IN ('sending','created','ambiguous','rejected','not_found','superseded')",
        name="ck_call_attempts_status",
    ),
)

provider_events = Table(
    "provider_events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("call_id", UUID(as_uuid=True), nullable=True),
    Column("provider", String(16), nullable=False),
    Column("event_id", String(128), nullable=False),
    Column("event_type", String(32), nullable=False),
    Column("provider_status", String(16), nullable=True),
    Column("sequence", Integer, nullable=True),
    # Whitelisted, non-PII provider fields only (status, duration, price, answered_by).
    Column("data", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("status", String(16), nullable=False, server_default="received"),
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("processed_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("tenant_id", "id", name="uq_provider_events_tenant_id"),
    UniqueConstraint("provider", "event_id", name="uq_provider_events_event"),
    CheckConstraint("status IN ('received','processed','ignored')", name="ck_provider_events_status"),
    Index("ix_provider_events_pending", "tenant_id", "received_at", postgresql_where=text("status = 'received'")),
)

inbound_numbers = Table(
    "inbound_numbers",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("e164", String(16), nullable=False),
    Column("provider_number_sid", String(64), nullable=True),
    Column("agent_id", UUID(as_uuid=True), nullable=False),
    Column("label", String(120), nullable=False, server_default=""),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("use_for_outbound", Boolean, nullable=False, server_default=text("true")),
    Column("business_hours_only", Boolean, nullable=False, server_default=text("false")),
    Column("handoff_e164", String(16), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_inbound_numbers_tenant_id"),
    UniqueConstraint("e164", name="uq_inbound_numbers_e164"),
    CheckConstraint("status IN ('active','disabled')", name="ck_inbound_numbers_status"),
)

call_outcomes = Table(
    "call_outcomes",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("call_id", UUID(as_uuid=True), nullable=False),
    Column("outcome", String(64), nullable=False),
    Column("qualified", Boolean, nullable=True),
    Column("source", String(32), nullable=False, server_default="ai-agent-service"),
    Column("source_event_id", UUID(as_uuid=True), nullable=True),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_call_outcomes_tenant_id"),
    UniqueConstraint("tenant_id", "call_id", name="uq_call_outcomes_call"),
    ForeignKeyConstraint(
        ["tenant_id", "call_id"], ["calls.tenant_id", "calls.id"], ondelete="CASCADE", name="fk_call_outcomes_call"
    ),
)

# ---------------- platform-scoped (ids only) ----------------
number_directory = Table(
    "number_directory",
    metadata,
    Column("e164", String(16), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("inbound_number_id", UUID(as_uuid=True), nullable=False, unique=True),
    Column("status", String(16), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

call_directory = Table(
    "call_directory",
    metadata,
    Column("call_id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("provider_call_id", String(64), nullable=True, unique=True),
    # Work index for the sweeper / reconciliation jobs (they cannot see tenant rows without a tenant).
    Column("next_check_at", DateTime(timezone=True), nullable=True),
    Column("check_reason", String(16), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "check_reason IS NULL OR check_reason IN ('dispatch','callbacks','reconcile','live')",
        name="ck_call_directory_reason",
    ),
    Index("ix_call_directory_due", "next_check_at", postgresql_where=text("next_check_at IS NOT NULL")),
)

PLATFORM_SCOPED = {
    "number_directory": "ids-only E.164 -> tenant index used to resolve inbound webhooks before a tenant is known",
    "call_directory": "ids-only call -> tenant index for status webhooks and cross-tenant sweepers",
}
TENANT_TABLES = ("calls", "call_attempts", "provider_events", "inbound_numbers", "call_outcomes")

for _name, _reason in PLATFORM_SCOPED.items():
    metadata.tables[_name].comment = f"platform_scoped: {_reason}"

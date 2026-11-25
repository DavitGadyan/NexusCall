"""SQLAlchemy Core tables owned by Audit Service. Only this service touches them (build.md §3.2).

`audit_events` is append-only and hash-chained (build.md §3.1P):
* one chain per tenant (`chain` = the tenant id) and one `platform` chain for events without a tenant;
* `hash = sha256(prev_hash || canonical_json(event fields))`, `chain_seq` is gap-free per chain;
* UPDATE / DELETE / TRUNCATE are blocked by triggers (also for the table owner) and absent from RLS policies.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
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

PLATFORM_CHAIN = "platform"

audit_events = Table(
    "audit_events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("event_id", UUID(as_uuid=True), nullable=False),
    # NULL = platform-level event (only insertable from a session without tenant context).
    Column("tenant_id", UUID(as_uuid=True), nullable=True),
    Column("chain", String(36), nullable=False),
    Column("chain_seq", BigInteger, nullable=False),
    Column("prev_hash", String(64), nullable=False),
    Column("hash", String(64), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("actor_type", String(16), nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("action", String(128), nullable=False),
    Column("resource_type", String(64), nullable=False),
    Column("resource_id", String(128), nullable=True),
    Column("outcome", String(16), nullable=False),
    Column("request_id", String(128), nullable=True),
    Column("correlation_id", UUID(as_uuid=True), nullable=True),
    Column("causation_id", UUID(as_uuid=True), nullable=True),
    Column("producer", String(64), nullable=False),
    Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    UniqueConstraint("event_id", name="uq_audit_events_event_id"),
    UniqueConstraint("tenant_id", "id", name="uq_audit_events_tenant_id"),
    UniqueConstraint("chain", "chain_seq", name="uq_audit_events_chain_seq"),
    CheckConstraint("actor_type IN ('user','service','system')", name="ck_audit_events_actor_type"),
    CheckConstraint("outcome IN ('success','denied','failure')", name="ck_audit_events_outcome"),
    CheckConstraint("chain_seq >= 1", name="ck_audit_events_chain_seq"),
    CheckConstraint(
        "(tenant_id IS NULL AND chain = 'platform') OR (tenant_id IS NOT NULL AND chain = tenant_id::text)",
        name="ck_audit_events_chain",
    ),
    Index("ix_audit_events_tenant_time", "tenant_id", "occurred_at", "id"),
    Index("ix_audit_events_tenant_action", "tenant_id", "action"),
    comment="append_only: tamper-evident, hash-chained audit trail",
)

PLATFORM_SCOPED: dict[str, str] = {}

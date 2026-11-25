"""SQLAlchemy Core tables owned by Tool Execution Service. Only this service touches them (build.md §3.2).

Tenant-owned (forced RLS): tool_execution_records (append-only, ids/enums/hashes only: never arguments,
outputs or credentials), ai_approval_requests (the ONLY source of truth for a Class 3 approval).
Platform-scoped: approval_tenants (ids-only index so the expiry job can visit tenants without bypassing RLS).
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    SmallInteger,
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

EXECUTION_STATUSES = ("executed", "approval_required", "denied", "failed")
APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired", "consumed")

tool_execution_records = Table(
    "tool_execution_records",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("tool_id", String(80), nullable=False),
    Column("tool_version", Integer, nullable=True),
    Column("risk_class", SmallInteger, nullable=True),
    Column("target_kind", String(16), nullable=True),
    Column("mcp_server_id", UUID(as_uuid=True), nullable=True),
    Column("agent_id", UUID(as_uuid=True), nullable=False),
    Column("actor_type", String(16), nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("caller", String(128), nullable=False),
    Column("call_id", UUID(as_uuid=True), nullable=True),
    Column("contact_id", UUID(as_uuid=True), nullable=True),
    Column("thread_id", UUID(as_uuid=True), nullable=True),
    Column("args_hash", String(64), nullable=True),
    Column("idempotency_key", String(255), nullable=True),
    Column("status", String(24), nullable=False),
    Column("reason", String(64), nullable=True),
    Column("approval_id", UUID(as_uuid=True), nullable=True),
    Column("policy_version", Integer, nullable=True),
    Column("duration_ms", Integer, nullable=False, server_default="0"),
    Column("output_bytes", Integer, nullable=False, server_default="0"),
    Column("correlation_id", String(128), nullable=True),
    Column("request_id", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_tool_execution_records_tenant_id"),
    CheckConstraint(
        "status IN ('executed','approval_required','denied','failed')", name="ck_tool_execution_records_status"
    ),
    Index("ix_tool_execution_records_recent", "tenant_id", "created_at"),
    comment="append_only: immutable tool execution trail (ids, enums and hashes only)",
)

ai_approval_requests = Table(
    "ai_approval_requests",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("tool_id", String(80), nullable=False),
    Column("tool_version", Integer, nullable=False),
    Column("risk_class", SmallInteger, nullable=False),
    Column("agent_id", UUID(as_uuid=True), nullable=False),
    # Canonical, schema-validated arguments: exactly what runs after approval (never re-read from the model).
    Column("arguments", JSONB, nullable=False),
    Column("args_hash", String(64), nullable=False),
    # Verified, server-bound context (ids only) and its hash.
    Column("context", JSONB, nullable=False),
    Column("context_hash", String(64), nullable=False),
    Column("requested_by_type", String(16), nullable=False),
    Column("requested_by_id", String(128), nullable=False),
    Column("caller", String(128), nullable=False),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("decided_by", UUID(as_uuid=True), nullable=True),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    Column("decision_note", String(500), nullable=True),
    Column("consumed_at", DateTime(timezone=True), nullable=True),
    Column("execution_record_id", UUID(as_uuid=True), nullable=True),
    Column("result_status", String(24), nullable=True),
    Column("result_reason", String(64), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_ai_approval_requests_tenant_id"),
    CheckConstraint(
        "status IN ('pending','approved','rejected','expired','consumed')", name="ck_ai_approval_requests_status"
    ),
    CheckConstraint(
        "status NOT IN ('approved','rejected','consumed') OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)",
        name="ck_ai_approval_requests_decided",
    ),
    CheckConstraint("(status = 'consumed') = (consumed_at IS NOT NULL)", name="ck_ai_approval_requests_consumed"),
    CheckConstraint("args_hash ~ '^[0-9a-f]{64}$' AND context_hash ~ '^[0-9a-f]{64}$'", name="ck_ai_approval_hashes"),
    # One open request per identical proposal: repeated proposals reuse it.
    Index(
        "uq_ai_approval_requests_open",
        "tenant_id",
        "tool_id",
        "agent_id",
        "args_hash",
        "context_hash",
        unique=True,
        postgresql_where=text("status = 'pending'"),
    ),
    Index("ix_ai_approval_requests_status", "tenant_id", "status", "created_at"),
)

approval_tenants = Table(
    "approval_tenants",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), primary_key=True),
    Column("last_request_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    comment="platform_scoped: ids-only index of tenants with approval requests (expiry job)",
)

PLATFORM_SCOPED = {"approval_tenants": "ids-only index of tenants with approval requests (expiry job)"}
TENANT_TABLES = ("tool_execution_records", "ai_approval_requests")

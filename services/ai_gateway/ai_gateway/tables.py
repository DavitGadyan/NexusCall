"""SQLAlchemy Core tables owned by the AI Gateway. Only this service touches them (build.md §3.2, §24.9).

Platform-scoped (catalogues): ai_models, routing_policies, ai_kill_switches.
Tenant-owned (RLS forced): tenant_model_allowlists, ai_budget_policies, ai_usage_records,
ai_requests (append-only), ai_security_events (append-only).

Nothing here stores prompt or completion text (OWASP LLM02): only sizes, hashes, counts and decisions.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
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

PLATFORM_SCOPED = {
    "ai_models": "platform model registry (approved models, prices, classifications)",
    "routing_policies": "platform routing rules per purpose",
    "ai_kill_switches": "platform-managed emergency switches (platform/provider/model/tenant/agent)",
}

ai_models = Table(
    "ai_models",
    metadata,
    Column("model_id", String(80), primary_key=True),
    Column("provider", String(32), nullable=False),
    Column("display_name", String(120), nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("input_price_micros_per_mtok", BigInteger, nullable=False),
    Column("output_price_micros_per_mtok", BigInteger, nullable=False),
    Column("max_output_tokens", Integer, nullable=False),
    Column("allowed_classifications", ARRAY(String(16)), nullable=False),
    Column("purposes", ARRAY(String(32)), nullable=False),
    Column("updated_by", UUID(as_uuid=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("provider IN ('anthropic','voyage')", name="ck_ai_models_provider"),
    comment=f"platform_scoped: {PLATFORM_SCOPED['ai_models']}",
)

routing_policies = Table(
    "routing_policies",
    metadata,
    Column("purpose", String(32), primary_key=True),
    Column("models", ARRAY(String(80)), nullable=False),  # ordered preference
    Column("failover_allowed", Boolean, nullable=False, server_default=text("false")),
    Column("updated_by", UUID(as_uuid=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    comment=f"platform_scoped: {PLATFORM_SCOPED['routing_policies']}",
)

ai_kill_switches = Table(
    "ai_kill_switches",
    metadata,
    Column("scope", String(16), nullable=False),
    Column("target", String(80), nullable=False),  # '*' for platform, provider/model id, tenant/agent uuid
    Column("engaged", Boolean, nullable=False),
    Column("reason", Text, nullable=False),
    Column("changed_by", UUID(as_uuid=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("scope", "target"),
    CheckConstraint("scope IN ('platform','provider','model','tenant','agent')", name="ck_kill_scope"),
    comment=f"platform_scoped: {PLATFORM_SCOPED['ai_kill_switches']}",
)

tenant_model_allowlists = Table(
    "tenant_model_allowlists",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("model_id", String(80), nullable=False),
    Column("created_by", UUID(as_uuid=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("tenant_id", "model_id"),
)

ai_budget_policies = Table(
    "ai_budget_policies",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("scope", String(16), nullable=False),  # tenant | agent
    Column("agent_id", UUID(as_uuid=True), nullable=True),
    Column("monthly_limit_micros", BigInteger, nullable=False),
    Column("per_request_limit_micros", BigInteger, nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("updated_by", UUID(as_uuid=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_budget_tenant_id"),
    CheckConstraint("scope IN ('tenant','agent')", name="ck_budget_scope"),
    CheckConstraint("(scope = 'agent') = (agent_id IS NOT NULL)", name="ck_budget_agent"),
    CheckConstraint("monthly_limit_micros >= 0", name="ck_budget_nonneg"),
    Index(
        "uq_budget_scope",
        "tenant_id",
        "scope",
        text("coalesce(agent_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
        unique=True,
    ),
)

ai_usage_records = Table(
    "ai_usage_records",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("period", String(7), nullable=False),  # YYYY-MM (UTC)
    Column("agent_key", String(36), nullable=False),  # agent uuid or '-' for requests without an agent
    Column("requests", BigInteger, nullable=False, server_default="0"),
    Column("input_tokens", BigInteger, nullable=False, server_default="0"),
    Column("output_tokens", BigInteger, nullable=False, server_default="0"),
    Column("cost_micros", BigInteger, nullable=False, server_default="0"),
    Column("denied", BigInteger, nullable=False, server_default="0"),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("tenant_id", "period", "agent_key"),
)

ai_requests = Table(
    "ai_requests",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("caller", String(80), nullable=False),
    Column("actor", String(160), nullable=False),
    Column("agent_id", UUID(as_uuid=True), nullable=True),
    Column("purpose", String(32), nullable=False),
    Column("classification", String(16), nullable=False),
    Column("model", String(80), nullable=True),
    Column("provider", String(32), nullable=True),
    Column("status", String(24), nullable=False),  # completed | denied | failed | blocked_output
    Column("deny_reason", String(64), nullable=True),
    Column("stop_reason", String(32), nullable=True),
    Column("rounds", Integer, nullable=False, server_default="0"),
    Column("tool_calls", JSONB, nullable=False, server_default=text("'[]'::jsonb")),  # ids + statuses only
    Column("input_chars", Integer, nullable=False, server_default="0"),
    Column("untrusted_blocks", Integer, nullable=False, server_default="0"),
    Column("input_tokens", Integer, nullable=False, server_default="0"),
    Column("output_tokens", Integer, nullable=False, server_default="0"),
    Column("cost_micros", BigInteger, nullable=False, server_default="0"),
    Column("latency_ms", Integer, nullable=False, server_default="0"),
    Column("flags", ARRAY(String(48)), nullable=False, server_default=text("'{}'")),
    Column("input_fingerprint", String(32), nullable=True),
    Column("correlation_id", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_ai_requests_tenant_time", "tenant_id", "created_at"),
    comment="append_only: AI request metadata (never prompt or completion text)",
)

ai_security_events = Table(
    "ai_security_events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("request_id", UUID(as_uuid=True), nullable=True),
    Column("kind", String(48), nullable=False),
    Column("severity", String(8), nullable=False),
    Column("owasp", String(8), nullable=False),
    Column("agent_id", UUID(as_uuid=True), nullable=True),
    Column("tool_id", String(80), nullable=True),
    Column("detail", String(500), nullable=False, server_default=""),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("severity IN ('info','low','medium','high')", name="ck_sec_severity"),
    Index("ix_ai_sec_tenant_time", "tenant_id", "created_at"),
    comment="append_only: AI security events (signals and enforced denials, no content)",
)

TENANT_TABLES = ("tenant_model_allowlists", "ai_budget_policies", "ai_usage_records")
APPEND_ONLY_TABLES = ("ai_requests", "ai_security_events")

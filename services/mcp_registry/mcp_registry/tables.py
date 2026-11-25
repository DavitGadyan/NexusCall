"""SQLAlchemy Core tables owned by MCP Registry / Tool Control Plane. Only this service touches them (build.md §3.2).

Platform-scoped (global catalogue, written only through `platform:ai:manage` + step-up): mcp_servers,
mcp_server_versions (append-only history), tool_definitions, ai_policy_versions, policy_proposals.
Tenant-owned (forced RLS): tenant_tool_settings (tighten-only overlay), agent_tool_grants.
No table stores a credential: MCP servers carry a Vault *reference* only.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
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


# ---------------------------------------------------------------- platform-scoped
mcp_servers = Table(
    "mcp_servers",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("key", String(63), nullable=False),
    Column("display_name", String(200), nullable=False),
    Column("description", Text, nullable=False, server_default=""),
    Column("owner", String(200), nullable=False),
    Column("purpose", Text, nullable=False),
    Column("endpoint", String(2048), nullable=False),
    Column("transport", String(32), nullable=False, server_default="streamable_http"),
    Column("environment", String(32), nullable=False),
    Column("trust_level", String(16), nullable=False),
    # Dedicated least-privilege identity of THIS server (never shared, never an admin identity).
    Column("service_identity", String(128), nullable=False),
    Column("vault_ref", String(255), nullable=True),  # Vault path of this server's own credential; never the value
    Column("allowed_hosts", ARRAY(String(253)), nullable=False),
    Column("timeout_ms", Integer, nullable=False, server_default="5000"),
    Column("max_concurrency", Integer, nullable=False, server_default="4"),
    Column("rate_per_minute", Integer, nullable=False, server_default="60"),
    Column("max_request_bytes", Integer, nullable=False, server_default="16384"),
    Column("max_response_bytes", Integer, nullable=False, server_default="65536"),
    # Supply-chain pin (owasp LLM04): reviewed release identity, version and artifact hash / image digest.
    Column("pinned_server_name", String(200), nullable=True),
    Column("pinned_version", String(64), nullable=True),
    Column("pinned_sha256", String(64), nullable=True),
    Column("reviewed_by", UUID(as_uuid=True), nullable=True),
    Column("reviewed_at", DateTime(timezone=True), nullable=True),
    Column("health", String(24), nullable=False, server_default="unknown"),
    Column("last_verified_at", DateTime(timezone=True), nullable=True),
    Column("enabled", Boolean, nullable=False, server_default=text("false")),
    Column("disabled_reason", String(200), nullable=True),
    Column("revoked", Boolean, nullable=False, server_default=text("false")),
    Column("revoked_reason", String(500), nullable=True),
    Column("created_by", UUID(as_uuid=True), nullable=True),
    Column("row_version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("key", name="uq_mcp_servers_key"),
    UniqueConstraint("service_identity", name="uq_mcp_servers_identity"),
    UniqueConstraint("vault_ref", name="uq_mcp_servers_vault_ref"),
    CheckConstraint("key ~ '^[a-z][a-z0-9-]{1,62}$'", name="ck_mcp_servers_key"),
    CheckConstraint("transport IN ('streamable_http')", name="ck_mcp_servers_transport"),
    CheckConstraint("trust_level IN ('internal','verified','third_party')", name="ck_mcp_servers_trust"),
    CheckConstraint(
        "health IN ('unknown','healthy','degraded','down','identity_mismatch')", name="ck_mcp_servers_health"
    ),
    CheckConstraint("pinned_sha256 IS NULL OR pinned_sha256 ~ '^[0-9a-f]{64}$'", name="ck_mcp_servers_sha256"),
    CheckConstraint(
        "NOT enabled OR (pinned_server_name IS NOT NULL AND pinned_version IS NOT NULL"
        " AND pinned_sha256 IS NOT NULL AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
        name="ck_mcp_servers_enabled_requires_pin",
    ),
    CheckConstraint("NOT (enabled AND revoked)", name="ck_mcp_servers_revoked_disabled"),
    CheckConstraint(
        "timeout_ms BETWEEN 100 AND 30000 AND max_concurrency BETWEEN 1 AND 64 AND rate_per_minute BETWEEN 1 AND 6000"
        " AND max_request_bytes BETWEEN 256 AND 262144 AND max_response_bytes BETWEEN 256 AND 1048576",
        name="ck_mcp_servers_limits",
    ),
    comment="platform_scoped: approved MCP server catalogue (vault references only)",
)

mcp_server_versions = Table(
    "mcp_server_versions",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("server_id", UUID(as_uuid=True), ForeignKey("mcp_servers.id"), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("change", String(32), nullable=False),
    Column("snapshot", JSONB, nullable=False),
    Column("changed_by", String(160), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("server_id", "revision", name="uq_mcp_server_versions_rev"),
    CheckConstraint(
        "change IN ('created','updated','pinned','enabled','disabled','revoked','auto_disabled')",
        name="ck_mcp_server_versions_change",
    ),
    comment="platform_scoped: append-only change history of MCP servers",
)

tool_definitions = Table(
    "tool_definitions",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tool_id", String(80), nullable=False),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("display_name", String(200), nullable=False),
    Column("description", Text, nullable=False),
    Column("kind", String(16), nullable=False),
    Column("server_id", UUID(as_uuid=True), ForeignKey("mcp_servers.id"), nullable=True),
    Column("target", JSONB, nullable=False),
    Column("input_schema", JSONB, nullable=False),
    Column("output_schema", JSONB, nullable=False),
    Column("risk_class", SmallInteger, nullable=False),
    Column("side_effecting", Boolean, nullable=False),
    Column("requires_approval", Boolean, nullable=False),
    Column("data_classification", String(16), nullable=False, server_default="confidential"),
    Column("rate_per_minute", Integer, nullable=False, server_default="60"),
    Column("timeout_ms", Integer, nullable=False, server_default="5000"),
    Column("max_output_bytes", Integer, nullable=False, server_default="32768"),
    Column("enabled", Boolean, nullable=False, server_default=text("false")),
    Column("disabled_reason", String(200), nullable=True),
    Column("revoked", Boolean, nullable=False, server_default=text("false")),
    Column("revoked_reason", String(500), nullable=True),
    Column("row_version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tool_id", name="uq_tool_definitions_tool_id"),
    CheckConstraint("tool_id ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$'", name="ck_tool_definitions_tool_id"),
    CheckConstraint("kind IN ('internal','mcp')", name="ck_tool_definitions_kind"),
    CheckConstraint("(kind = 'mcp') = (server_id IS NOT NULL)", name="ck_tool_definitions_server"),
    CheckConstraint("risk_class BETWEEN 0 AND 4", name="ck_tool_definitions_risk"),
    # Risk classes are minimum controls (build.md §24.3): class 3+ always needs approval, class 2+ writes.
    CheckConstraint("risk_class < 3 OR requires_approval", name="ck_tool_definitions_class3_approval"),
    CheckConstraint("risk_class < 2 OR side_effecting", name="ck_tool_definitions_class2_side_effect"),
    CheckConstraint(
        "data_classification IN ('public','internal','confidential','restricted')", name="ck_tool_definitions_class"
    ),
    CheckConstraint("NOT (enabled AND revoked)", name="ck_tool_definitions_revoked_disabled"),
    CheckConstraint(
        "rate_per_minute BETWEEN 1 AND 6000 AND timeout_ms BETWEEN 100 AND 30000"
        " AND max_output_bytes BETWEEN 256 AND 1048576",
        name="ck_tool_definitions_limits",
    ),
    comment="platform_scoped: registered tool catalogue with versioned JSON Schemas and risk classes",
)

ai_policy_versions = Table(
    "ai_policy_versions",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("version", Integer, nullable=False),
    Column("policy", JSONB, nullable=False),
    Column("status", String(16), nullable=False),
    Column("proposal_id", UUID(as_uuid=True), nullable=True),
    Column("rollback_of", Integer, nullable=True),
    Column("deployed_by", String(160), nullable=False),
    Column("deployed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("version", name="uq_ai_policy_versions_version"),
    CheckConstraint("status IN ('active','superseded')", name="ck_ai_policy_versions_status"),
    Index("uq_ai_policy_versions_active", "status", unique=True, postgresql_where=text("status = 'active'")),
    comment="platform_scoped: deployed tool policy versions (one active)",
)

policy_proposals = Table(
    "policy_proposals",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("base_version", Integer, nullable=False),
    Column("policy", JSONB, nullable=False),
    Column("reason", Text, nullable=False),
    Column("proposed_by", UUID(as_uuid=True), nullable=False),
    Column("approved_by", UUID(as_uuid=True), nullable=True),
    Column("rejected_by", UUID(as_uuid=True), nullable=True),
    Column("deployed_by", UUID(as_uuid=True), nullable=True),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("decision_note", String(500), nullable=True),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    Column("deployed_at", DateTime(timezone=True), nullable=True),
    Column("deployed_version", Integer, nullable=True),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    *_ts(),
    CheckConstraint(
        "status IN ('pending','approved','rejected','deployed','expired')", name="ck_policy_proposals_status"
    ),
    # Dual control (build.md §24.8): the approver must differ from the proposer.
    CheckConstraint("approved_by IS NULL OR approved_by <> proposed_by", name="ck_policy_proposals_dual_control"),
    comment="platform_scoped: reviewed policy change proposals (dual approval)",
)

# ---------------------------------------------------------------- tenant-owned (RLS)
tenant_tool_settings = Table(
    "tenant_tool_settings",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("tool_id", String(80), ForeignKey("tool_definitions.tool_id"), nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("requires_approval", Boolean, nullable=False, server_default=text("false")),
    Column("rate_per_minute", Integer, nullable=True),
    Column("updated_by", String(160), nullable=True),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_tenant_tool_settings_tenant_id"),
    UniqueConstraint("tenant_id", "tool_id", name="uq_tenant_tool_settings_tool"),
    CheckConstraint("rate_per_minute IS NULL OR rate_per_minute BETWEEN 1 AND 6000", name="ck_tts_rate"),
)

agent_tool_grants = Table(
    "agent_tool_grants",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("agent_id", UUID(as_uuid=True), nullable=False),
    Column("tool_id", String(80), ForeignKey("tool_definitions.tool_id"), nullable=False),
    Column("requires_approval", Boolean, nullable=False, server_default=text("false")),
    Column("calls_per_minute", Integer, nullable=True),
    Column("granted_by", String(160), nullable=False),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_agent_tool_grants_tenant_id"),
    UniqueConstraint("tenant_id", "agent_id", "tool_id", name="uq_agent_tool_grants_tool"),
    CheckConstraint("calls_per_minute IS NULL OR calls_per_minute BETWEEN 1 AND 6000", name="ck_atg_rate"),
)

PLATFORM_SCOPED = {
    "mcp_servers": "approved MCP server catalogue (vault references only)",
    "mcp_server_versions": "append-only change history of MCP servers",
    "tool_definitions": "registered tool catalogue with versioned JSON Schemas and risk classes",
    "ai_policy_versions": "deployed tool policy versions (one active)",
    "policy_proposals": "reviewed policy change proposals (dual approval)",
}
TENANT_TABLES = ("tenant_tool_settings", "agent_tool_grants")

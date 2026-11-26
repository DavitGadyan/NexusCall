"""SQLAlchemy Core tables owned by Integration & Webhook Service. Only this service touches them (build.md §3.2).

NO TABLE HERE EVER HOLDS A SECRET VALUE. Credentials live in Vault; rows carry the Vault path (`vault_ref`),
masked identifiers and lifecycle metadata only (build.md §19.2, ADR-0009).

Tenant-owned (RLS forced): provider_connections, webhook_endpoints, webhook_deliveries,
webhook_delivery_attempts (append-only), inbound_webhook_events.
Platform-scoped: oauth_states (state lookup for the unauthenticated callback; hashes only), secret_registry
(metadata index of every Vault path this service manages), platform_connections, inbound_webhook_registry
(trusted mapping provider account → tenant for inbound webhooks), integration_tenants (ids-only index).
"""

from __future__ import annotations

from sqlalchemy import (
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from nexus_core.tables import idempotency_table, inbox_table, outbox_table

metadata = MetaData()

outbox = outbox_table(metadata)
inbox = inbox_table(metadata)
idempotency_keys = idempotency_table(metadata)

CONNECTION_STATUSES = ("active", "disabled", "revoked", "error")


def _ts() -> list[Column]:  # type: ignore[type-arg]
    return [
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ]


def _connection_columns() -> list[Column]:  # type: ignore[type-arg]
    return [
        Column("provider", String(32), nullable=False),
        Column("display_name", String(120), nullable=True),
        Column("status", String(16), nullable=False, server_default="error"),
        Column("auth_type", String(16), nullable=False, server_default="static"),
        Column("vault_ref", String(255), nullable=False),
        Column("vault_version", Integer, nullable=True),
        # Masked provider account identifier (e.g. "AC…3f2a", "d…@example.com"). Never the credential.
        Column("account_masked", String(120), nullable=True),
        Column("granted_scopes", ARRAY(String(200)), nullable=False, server_default=text("'{}'")),
        # Non-secret configuration (allow-listed keys per provider, e.g. region or calendar id).
        Column("config", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("last_error", String(64), nullable=True),
        Column("last_validated_at", DateTime(timezone=True), nullable=True),
        Column("last_rotated_at", DateTime(timezone=True), nullable=True),
        Column("next_rotation_due", DateTime(timezone=True), nullable=True),
        Column("last_vault_status", String(32), nullable=True),
        Column("last_resolved_at", DateTime(timezone=True), nullable=True),
        Column("created_by", String(140), nullable=False),
        Column("version", Integer, nullable=False, server_default="1"),
        *_ts(),
    ]


# ---------------- platform-scoped ----------------
integration_tenants = Table(
    "integration_tenants",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), primary_key=True),
    Column("first_seen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

oauth_states = Table(
    "oauth_states",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    # SHA-256 of the raw `state` (the raw value exists only in the authorization URL) and of the browser
    # binding cookie. The PKCE verifier is derived from the raw state with a server key, so it is never stored.
    Column("state_hash", String(64), nullable=False, unique=True),
    Column("binding_hash", String(64), nullable=False),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("provider", String(32), nullable=False),
    Column("scope_key", String(40), nullable=False, server_default="default"),
    Column("redirect_uri", String(500), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("used_at", DateTime(timezone=True), nullable=True),
    Column("outcome", String(32), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_oauth_states_expiry", "expires_at"),
)

secret_registry = Table(
    "secret_registry",
    metadata,
    Column("vault_ref", String(255), primary_key=True),
    Column("scope", String(16), nullable=False),  # tenant | platform
    Column("tenant_id", UUID(as_uuid=True), nullable=True),
    Column("connection_id", UUID(as_uuid=True), nullable=True),
    Column("owning_service", String(64), nullable=False),
    Column("provider", String(32), nullable=False),
    Column("kind", String(24), nullable=False),  # provider_credential | webhook_signing | oauth_client
    Column("environment", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("vault_version", Integer, nullable=True),
    Column("last_rotated_at", DateTime(timezone=True), nullable=True),
    Column("next_rotation_due", DateTime(timezone=True), nullable=True),
    Column("rotation_reminded_at", DateTime(timezone=True), nullable=True),
    Column("last_retrieval_at", DateTime(timezone=True), nullable=True),
    Column("last_vault_status", String(32), nullable=True),
    Column("last_checked_at", DateTime(timezone=True), nullable=True),
    *_ts(),
    CheckConstraint("scope IN ('tenant','platform')", name="ck_secret_registry_scope"),
    CheckConstraint("scope = 'platform' OR tenant_id IS NOT NULL", name="ck_secret_registry_tenant"),
    Index("ix_secret_registry_rotation", "next_rotation_due"),
)

platform_connections = Table(
    "platform_connections",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("owning_service", String(64), nullable=False),
    *_connection_columns(),
    CheckConstraint("status IN ('active','disabled','revoked','error')", name="ck_platform_connections_status"),
    Index(
        "uq_platform_connections_live",
        "owning_service",
        "provider",
        unique=True,
        postgresql_where=text("status <> 'revoked'"),
    ),
)

inbound_webhook_registry = Table(
    "inbound_webhook_registry",
    metadata,
    Column("provider", String(32), nullable=False, primary_key=True),
    Column("external_account_id", String(64), nullable=False, primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("connection_id", UUID(as_uuid=True), nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    *_ts(),
)

# ---------------- tenant-owned (RLS) ----------------
provider_connections = Table(
    "provider_connections",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    # Workspace scope label; resolution uses "default". Lets a tenant hold e.g. one Twilio account per brand.
    Column("scope", String(40), nullable=False, server_default="default"),
    *_connection_columns(),
    UniqueConstraint("tenant_id", "id", name="uq_provider_connections_tenant_id"),
    CheckConstraint("status IN ('active','disabled','revoked','error')", name="ck_provider_connections_status"),
    CheckConstraint("auth_type IN ('static','oauth')", name="ck_provider_connections_auth_type"),
    Index(
        "uq_provider_connections_live",
        "tenant_id",
        "provider",
        "scope",
        unique=True,
        postgresql_where=text("status <> 'revoked'"),
    ),
)

webhook_endpoints = Table(
    "webhook_endpoints",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("url", String(2048), nullable=False),
    Column("description", String(200), nullable=True),
    Column("event_types", ARRAY(String(64)), nullable=False),
    Column("status", String(16), nullable=False, server_default="active"),
    # Vault path of the HMAC signing secret (the secret itself never touches this database).
    Column("secret_ref", String(255), nullable=False),
    Column("secret_version", Integer, nullable=True),
    Column("consecutive_failures", Integer, nullable=False, server_default="0"),
    Column("last_delivery_at", DateTime(timezone=True), nullable=True),
    Column("created_by", String(140), nullable=False),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_webhook_endpoints_tenant_id"),
    CheckConstraint("status IN ('active','disabled','deleted')", name="ck_webhook_endpoints_status"),
)

webhook_deliveries = Table(
    "webhook_deliveries",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("endpoint_id", UUID(as_uuid=True), nullable=False),
    Column("event_id", UUID(as_uuid=True), nullable=False),
    Column("event_type", String(128), nullable=False),
    # The delivered body: the domain event envelope, which by contract carries ids and enums only.
    Column("payload", JSONB, nullable=False),
    Column("status", String(16), nullable=False, server_default="queued"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("lease_until", DateTime(timezone=True), nullable=True),
    Column("enqueued_at", DateTime(timezone=True), nullable=True),
    Column("last_status_code", Integer, nullable=True),
    Column("last_error", String(64), nullable=True),
    Column("delivered_at", DateTime(timezone=True), nullable=True),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_webhook_deliveries_tenant_id"),
    UniqueConstraint("tenant_id", "endpoint_id", "event_id", name="uq_webhook_deliveries_dedupe"),
    ForeignKeyConstraint(
        ["tenant_id", "endpoint_id"],
        ["webhook_endpoints.tenant_id", "webhook_endpoints.id"],
        ondelete="CASCADE",
        name="fk_webhook_deliveries_endpoint",
    ),
    CheckConstraint(
        "status IN ('queued','delivering','succeeded','failed','canceled')", name="ck_webhook_deliveries_status"
    ),
    Index("ix_webhook_deliveries_due", "next_attempt_at", postgresql_where=text("status IN ('queued','delivering')")),
    Index("ix_webhook_deliveries_endpoint", "tenant_id", "endpoint_id", "created_at"),
)

webhook_delivery_attempts = Table(
    "webhook_delivery_attempts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("delivery_id", UUID(as_uuid=True), nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("outcome", String(16), nullable=False),  # succeeded | failed | blocked
    Column("status_code", Integer, nullable=True),
    Column("error", String(64), nullable=True),
    Column("duration_ms", Integer, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["tenant_id", "delivery_id"],
        ["webhook_deliveries.tenant_id", "webhook_deliveries.id"],
        ondelete="CASCADE",
        name="fk_webhook_attempts_delivery",
    ),
    Index("ix_webhook_attempts_delivery", "tenant_id", "delivery_id", "attempt"),
    comment="append_only: immutable delivery attempt log",
)

inbound_webhook_events = Table(
    "inbound_webhook_events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("provider", String(32), nullable=False),
    Column("connection_id", UUID(as_uuid=True), nullable=False),
    Column("provider_event_id", String(128), nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("object_id", String(64), nullable=True),
    Column("occurred_at", DateTime(timezone=True), nullable=True),
    Column("status", String(16), nullable=False, server_default="received"),
    *_ts(),
    UniqueConstraint("tenant_id", "provider", "provider_event_id", name="uq_inbound_webhook_events_dedupe"),
    CheckConstraint("status IN ('received','processed','ignored')", name="ck_inbound_webhook_events_status"),
)

PLATFORM_SCOPED = {
    "integration_tenants": "ids-only index of tenants with integration data (periodic jobs)",
    "oauth_states": "OAuth state lookup for the unauthenticated callback (hashes only, short-lived)",
    "secret_registry": "metadata index of Vault paths managed by integration-service (never values)",
    "platform_connections": "platform-level provider connection metadata (secrets live in Vault)",
    "inbound_webhook_registry": "trusted provider-account to tenant mapping for inbound webhooks",
}
TENANT_TABLES = (
    "provider_connections",
    "webhook_endpoints",
    "webhook_deliveries",
    "webhook_delivery_attempts",
    "inbound_webhook_events",
)

for _name, _reason in PLATFORM_SCOPED.items():
    metadata.tables[_name].comment = f"platform_scoped: {_reason}"

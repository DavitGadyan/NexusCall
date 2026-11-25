"""SQLAlchemy Core tables owned by tenant-service. Only this service touches them (build.md §3.2).

Platform-scoped (no tenant_id, marked in migration): users, tenants, permissions, roles, role_permissions,
membership_directory, invitation_tokens, platform_settings, feature_flags, service_accounts, rls_scans.
Tenant-owned (RLS forced): memberships, membership_roles, teams, invitations, tenant_settings,
tenant_feature_overrides, support_grants.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
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


# ---------------- platform-scoped ----------------
users = Table(
    "users",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),  # == IdP subject
    Column("email", String(320), nullable=False),
    Column("display_name", String(200), nullable=True),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("last_seen_at", DateTime(timezone=True), nullable=True),
    *_ts(),
    UniqueConstraint("email", name="uq_users_email"),
    CheckConstraint("status IN ('active','deactivated')", name="ck_users_status"),
)

tenants = Table(
    "tenants",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("slug", String(63), nullable=False),
    Column("name", String(200), nullable=False),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("data_region", String(32), nullable=False, server_default="default"),
    Column("created_by", UUID(as_uuid=True), nullable=True),
    Column("status_reason", Text, nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("slug", name="uq_tenants_slug"),
    CheckConstraint("status IN ('active','suspended','archived')", name="ck_tenants_status"),
    CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'", name="ck_tenants_slug"),
)

permissions = Table(
    "permissions",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("scope", String(16), nullable=False),
    Column("description", Text, nullable=False),
)

roles = Table(
    "roles",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("key", String(64), nullable=False),
    Column("name", String(120), nullable=False),
    Column("scope", String(16), nullable=False),
    Column("description", Text, nullable=False, server_default=""),
    Column("is_system", Boolean, nullable=False, server_default=text("false")),
    Column("requires_mfa", Boolean, nullable=False, server_default=text("false")),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("key", name="uq_roles_key"),
    CheckConstraint("scope IN ('tenant','platform')", name="ck_roles_scope"),
)

role_permissions = Table(
    "role_permissions",
    metadata,
    Column("role_id", UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
    Column("permission_key", String(64), ForeignKey("permissions.key"), nullable=False),
    PrimaryKeyConstraint("role_id", "permission_key"),
)

membership_directory = Table(  # IDs-only index so a user can list their tenants without cross-tenant reads
    "membership_directory",
    metadata,
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id"), nullable=False),
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False),
    Column("membership_id", UUID(as_uuid=True), nullable=False),
    Column("status", String(16), nullable=False),
    PrimaryKeyConstraint("user_id", "tenant_id"),
)

invitation_tokens = Table(  # token hash -> (tenant, invitation) lookup for the unauthenticated accept step
    "invitation_tokens",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("invitation_id", UUID(as_uuid=True), nullable=False, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

platform_role_assignments = Table(
    "platform_role_assignments",
    metadata,
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id"), nullable=False),
    Column("role_key", String(64), nullable=False),
    Column("granted_by", UUID(as_uuid=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("user_id", "role_key"),
)

platform_settings = Table(
    "platform_settings",
    metadata,
    Column("section", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("data", JSONB, nullable=False),
    Column("is_current", Boolean, nullable=False, server_default=text("true")),
    Column("changed_by", UUID(as_uuid=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("section", "version"),
    Index("uq_platform_settings_current", "section", unique=True, postgresql_where=text("is_current")),
)

feature_flags = Table(
    "feature_flags",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("description", Text, nullable=False, server_default=""),
    Column("enabled", Boolean, nullable=False, server_default=text("false")),
    Column("tenant_overridable", Boolean, nullable=False, server_default=text("true")),
    Column("environments", ARRAY(String(16)), nullable=False, server_default=text("'{}'")),
    *_ts(),
)

service_accounts = Table(
    "service_accounts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("name", String(120), nullable=False),
    Column("client_id", String(120), nullable=False, unique=True),
    Column("owner_user_id", UUID(as_uuid=True), ForeignKey("users.id"), nullable=False),
    Column("purpose", Text, nullable=False),
    Column("tenant_id", UUID(as_uuid=True), nullable=True),  # set for tenant-scoped integration accounts
    Column("scopes", ARRAY(String(64)), nullable=False, server_default=text("'{}'")),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("review_at", DateTime(timezone=True), nullable=False),
    Column("secret_ref", String(255), nullable=True),  # Vault path, never the secret
    Column("last_rotated_at", DateTime(timezone=True), nullable=True),
    *_ts(),
    CheckConstraint("status IN ('active','disabled')", name="ck_sa_status"),
)

privileged_requests = Table(  # dual-approval queue for critical platform changes (build.md §20.2)
    "privileged_requests",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("kind", String(64), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("reason", Text, nullable=False),
    Column("requested_by", UUID(as_uuid=True), nullable=False),
    Column("approved_by", UUID(as_uuid=True), nullable=True),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    *_ts(),
    CheckConstraint("status IN ('pending','approved','rejected','expired')", name="ck_priv_status"),
    CheckConstraint("approved_by IS NULL OR approved_by <> requested_by", name="ck_priv_dual_control"),
)

rls_scans = Table(
    "rls_scans",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("started_by", UUID(as_uuid=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("summary", JSONB, nullable=False),
    Column("results", JSONB, nullable=False),
)

# ---------------- tenant-owned (RLS) ----------------
teams = Table(
    "teams",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False),
    Column("name", String(120), nullable=False),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_teams_tenant_id"),
    UniqueConstraint("tenant_id", "name", name="uq_teams_name"),
)

memberships = Table(
    "memberships",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id"), nullable=False),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("team_id", UUID(as_uuid=True), nullable=True),
    Column("invited_by", UUID(as_uuid=True), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_memberships_tenant_id"),
    UniqueConstraint("tenant_id", "user_id", name="uq_memberships_user"),
    ForeignKeyConstraint(["tenant_id", "team_id"], ["teams.tenant_id", "teams.id"], name="fk_memberships_team"),
    CheckConstraint("status IN ('active','deactivated')", name="ck_memberships_status"),
)

membership_roles = Table(
    "membership_roles",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("membership_id", UUID(as_uuid=True), nullable=False),
    Column("role_id", UUID(as_uuid=True), ForeignKey("roles.id"), nullable=False),
    Column("granted_by", UUID(as_uuid=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("tenant_id", "membership_id", "role_id"),
    ForeignKeyConstraint(
        ["tenant_id", "membership_id"],
        ["memberships.tenant_id", "memberships.id"],
        ondelete="CASCADE",
        name="fk_membership_roles_membership",
    ),
)

invitations = Table(
    "invitations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False),
    Column("email", String(320), nullable=False),
    Column("role_keys", ARRAY(String(64)), nullable=False),
    Column("team_id", UUID(as_uuid=True), nullable=True),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("invited_by", UUID(as_uuid=True), nullable=True),
    Column("accepted_by", UUID(as_uuid=True), nullable=True),
    Column("accepted_at", DateTime(timezone=True), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    Column("sent_at", DateTime(timezone=True), nullable=True),
    Column("superseded_by", UUID(as_uuid=True), nullable=True),
    Column("delivery_error", String(200), nullable=True),
    Column("send_count", Integer, nullable=False, server_default="0"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_invitations_tenant_id"),
    CheckConstraint(
        "status IN ('pending','sent','accepted','expired','revoked','delivery_failed')", name="ck_invitations_status"
    ),
    Index(
        "uq_invitations_open_per_email",
        "tenant_id",
        "email",
        unique=True,
        postgresql_where=text("status IN ('pending','sent','delivery_failed')"),
    ),
)

tenant_settings = Table(
    "tenant_settings",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False),
    Column("section", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("data", JSONB, nullable=False),
    Column("is_current", Boolean, nullable=False, server_default=text("true")),
    Column("changed_by", UUID(as_uuid=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("tenant_id", "section", "version"),
    Index("uq_tenant_settings_current", "tenant_id", "section", unique=True, postgresql_where=text("is_current")),
)

tenant_feature_overrides = Table(
    "tenant_feature_overrides",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False),
    Column("flag_key", String(64), ForeignKey("feature_flags.key"), nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("changed_by", UUID(as_uuid=True), nullable=True),
    *_ts(),
    PrimaryKeyConstraint("tenant_id", "flag_key"),
)

support_grants = Table(
    "support_grants",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False),
    Column("requested_by", UUID(as_uuid=True), nullable=False),
    Column("approved_by", UUID(as_uuid=True), nullable=True),
    Column("scope", String(32), nullable=False),
    Column("reason", Text, nullable=False),
    Column("ticket_ref", String(64), nullable=False),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    *_ts(),
    CheckConstraint("status IN ('pending','active','rejected','revoked','expired')", name="ck_support_status"),
    CheckConstraint("approved_by IS NULL OR approved_by <> requested_by", name="ck_support_dual_control"),
)

PLATFORM_SCOPED = {
    "users": "identity directory (IdP subjects)",
    "tenants": "tenant registry",
    "permissions": "code-defined permission catalogue",
    "roles": "role catalogue composed from the permission catalogue",
    "role_permissions": "role catalogue mapping",
    "membership_directory": "ids-only index of user->tenant memberships",
    "invitation_tokens": "token-hash lookup for invitation acceptance (no PII)",
    "platform_role_assignments": "platform admin assignments (mirrors IdP realm roles)",
    "platform_settings": "platform-wide settings",
    "feature_flags": "platform feature flag definitions",
    "service_accounts": "non-human identity metadata (secrets live in Vault/IdP)",
    "rls_scans": "RLS coverage scan history",
    "privileged_requests": "dual-approval queue for platform-level changes",
}
TENANT_TABLES = (
    "teams",
    "memberships",
    "membership_roles",
    "invitations",
    "tenant_settings",
    "tenant_feature_overrides",
    "support_grants",
)

for _name, _reason in PLATFORM_SCOPED.items():
    metadata.tables[_name].comment = f"platform_scoped: {_reason}"

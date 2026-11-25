"""SQLAlchemy Core tables owned by Notification Service. Only this service touches them (build.md §3.2).

* `notifications`: in-app notifications addressed to one user OR to everyone holding a permission.
* `notification_reads`: per-user read markers (permission-audience notifications are read per user).
* `notification_preferences`: per-user, per-tenant preferences.
* `email_log`: append-only delivery log for operational mail. No bodies, no addresses: a recipient hash only.
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
    PrimaryKeyConstraint,
    String,
    Table,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from nexus_core.tables import idempotency_table, inbox_table, outbox_table

metadata = MetaData()

outbox = outbox_table(metadata)
inbox = inbox_table(metadata)
idempotency_keys = idempotency_table(metadata)

notifications = Table(
    "notifications",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("user_id", UUID(as_uuid=True), nullable=True),
    Column("audience_permission", String(64), nullable=True),
    Column("kind", String(32), nullable=False),
    Column("title", String(200), nullable=False),
    Column("body", String(1000), nullable=False),
    Column("link", String(300), nullable=True),
    # Dedupe for event-driven and idempotent internal creation (e.g. `event:<event_id>`).
    Column("dedupe_key", String(160), nullable=True),
    Column("source", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_notifications_tenant_id"),
    UniqueConstraint("tenant_id", "dedupe_key", name="uq_notifications_dedupe"),
    CheckConstraint("(user_id IS NULL) <> (audience_permission IS NULL)", name="ck_notifications_one_audience"),
    CheckConstraint("kind ~ '^[a-z][a-z0-9_.]{1,31}$'", name="ck_notifications_kind"),
    CheckConstraint("link IS NULL OR link ~ '^/[A-Za-z0-9/_?=&.%-]*$'", name="ck_notifications_link"),
    Index("ix_notifications_user", "tenant_id", "user_id", "created_at"),
    Index("ix_notifications_permission", "tenant_id", "audience_permission", "created_at"),
)

notification_reads = Table(
    "notification_reads",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("notification_id", UUID(as_uuid=True), nullable=False),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("read_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("tenant_id", "notification_id", "user_id"),
    ForeignKeyConstraint(
        ["tenant_id", "notification_id"],
        ["notifications.tenant_id", "notifications.id"],
        ondelete="CASCADE",
        name="fk_notification_reads_notification",
    ),
    Index("ix_notification_reads_user", "tenant_id", "user_id"),
)

notification_preferences = Table(
    "notification_preferences",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("email_enabled", Boolean, nullable=False, server_default=text("true")),
    Column("muted_kinds", ARRAY(String(32)), nullable=False, server_default=text("'{}'")),
    Column("version", Integer, nullable=False, server_default=text("1")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_notification_preferences_tenant_id"),
    UniqueConstraint("tenant_id", "user_id", name="uq_notification_preferences_user"),
)

email_log = Table(
    "email_log",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    # NULL for platform-level mail (e.g. the owner invitation of a brand-new tenant).
    Column("tenant_id", UUID(as_uuid=True), nullable=True),
    Column("template", String(64), nullable=False),
    Column("category", String(16), nullable=False),
    Column("recipient_hash", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("provider", String(16), nullable=False),
    Column("provider_message_id", String(255), nullable=True),
    Column("error_code", String(64), nullable=True),
    Column("requested_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_email_log_tenant_id"),
    CheckConstraint("status IN ('sent','failed','rejected','rate_limited')", name="ck_email_log_status"),
    CheckConstraint("category IN ('transactional')", name="ck_email_log_category"),
    Index("ix_email_log_recipient", "recipient_hash", "created_at"),
    comment="append_only: operational email delivery log (no bodies, hashed recipients)",
)

PLATFORM_SCOPED: dict[str, str] = {}
TENANT_TABLES = ("notifications", "notification_reads", "notification_preferences")

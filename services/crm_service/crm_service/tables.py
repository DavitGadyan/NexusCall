"""SQLAlchemy Core tables owned by CRM Service. Only this service touches them (build.md §3.2, §7).

Tenant-owned (forced RLS): contacts, organizations, leads, tags, contact_tags, custom_field_defs,
consent_records (append-only), suppressions, contact_notes, imports, import_rows, segments, exports.
Platform-scoped: crm_jobs (ids-only index so the sweeper can find stuck background jobs).
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
    Text,
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


def _id() -> Column:  # type: ignore[type-arg]
    return Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


def _tenant() -> Column:  # type: ignore[type-arg]
    return Column("tenant_id", UUID(as_uuid=True), nullable=False)


def _ts() -> list[Column]:  # type: ignore[type-arg]
    return [
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ]


organizations = Table(
    "organizations",
    metadata,
    _id(),
    _tenant(),
    Column("name", String(200), nullable=False),
    Column("domain", String(253), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_organizations_tenant_id"),
    UniqueConstraint("tenant_id", "name", name="uq_organizations_name"),
)

contacts = Table(
    "contacts",
    metadata,
    _id(),
    _tenant(),
    Column("first_name", String(100), nullable=True),
    Column("last_name", String(100), nullable=True),
    Column("company", String(200), nullable=True),
    Column("organization_id", UUID(as_uuid=True), nullable=True),
    Column("email", String(320), nullable=True),  # normalized (trimmed, lower-case)
    Column("phone_e164", String(16), nullable=True),
    Column("timezone", String(64), nullable=True),
    Column("country", String(2), nullable=True),
    Column("locale", String(16), nullable=True),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("owner_id", UUID(as_uuid=True), nullable=True),
    Column("source", String(64), nullable=True),
    Column("custom_fields", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_by", String(140), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_contacts_tenant_id"),
    ForeignKeyConstraint(
        ["tenant_id", "organization_id"],
        ["organizations.tenant_id", "organizations.id"],
        name="fk_contacts_organization",
    ),
    CheckConstraint("status IN ('active','inactive','archived')", name="ck_contacts_status"),
    CheckConstraint("phone_e164 ~ '^\\+[1-9][0-9]{6,14}$'", name="ck_contacts_phone_e164"),
    CheckConstraint("email = lower(email)", name="ck_contacts_email_normalized"),
    Index("uq_contacts_email", "tenant_id", "email", unique=True, postgresql_where=text("email IS NOT NULL")),
    Index("uq_contacts_phone", "tenant_id", "phone_e164", unique=True, postgresql_where=text("phone_e164 IS NOT NULL")),
    Index("ix_contacts_created", "tenant_id", "created_at", "id"),
)

leads = Table(
    "leads",
    metadata,
    _id(),
    _tenant(),
    Column("contact_id", UUID(as_uuid=True), nullable=False),
    Column("status", String(16), nullable=False, server_default="new"),
    Column("source", String(64), nullable=True),
    # Attribution ids (other contexts' ids, never joined across databases).
    Column("campaign_id", UUID(as_uuid=True), nullable=True),
    Column("call_id", UUID(as_uuid=True), nullable=True),
    Column("booking_id", UUID(as_uuid=True), nullable=True),
    Column("owner_id", UUID(as_uuid=True), nullable=True),
    Column("score", Integer, nullable=True),
    Column("qualified", Boolean, nullable=True),
    Column("last_outcome", String(64), nullable=True),
    Column("created_by", String(140), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_leads_tenant_id"),
    ForeignKeyConstraint(
        ["tenant_id", "contact_id"], ["contacts.tenant_id", "contacts.id"], ondelete="CASCADE", name="fk_leads_contact"
    ),
    CheckConstraint(
        "status IN ('new','contacted','qualified','unqualified','booked','won','lost')", name="ck_leads_status"
    ),
    CheckConstraint("score IS NULL OR (score >= 0 AND score <= 100)", name="ck_leads_score"),
    Index("ix_leads_contact", "tenant_id", "contact_id"),
    Index("ix_leads_created", "tenant_id", "created_at", "id"),
)

tags = Table(
    "tags",
    metadata,
    _id(),
    _tenant(),
    Column("name", String(64), nullable=False),
    Column("color", String(7), nullable=True),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_tags_tenant_id"),
    UniqueConstraint("tenant_id", "name", name="uq_tags_name"),
    CheckConstraint("color IS NULL OR color ~ '^#[0-9a-fA-F]{6}$'", name="ck_tags_color"),
)

contact_tags = Table(
    "contact_tags",
    metadata,
    _id(),
    _tenant(),
    Column("contact_id", UUID(as_uuid=True), nullable=False),
    Column("tag_id", UUID(as_uuid=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_contact_tags_tenant_id"),
    UniqueConstraint("tenant_id", "contact_id", "tag_id", name="uq_contact_tags_pair"),
    ForeignKeyConstraint(
        ["tenant_id", "contact_id"],
        ["contacts.tenant_id", "contacts.id"],
        ondelete="CASCADE",
        name="fk_contact_tags_contact",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "tag_id"], ["tags.tenant_id", "tags.id"], ondelete="CASCADE", name="fk_contact_tags_tag"
    ),
)

custom_field_defs = Table(
    "custom_field_defs",
    metadata,
    _id(),
    _tenant(),
    Column("key", String(64), nullable=False),
    Column("label", String(120), nullable=False),
    Column("type", String(16), nullable=False),
    Column("options", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("required", Boolean, nullable=False, server_default=text("false")),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_custom_field_defs_tenant_id"),
    UniqueConstraint("tenant_id", "key", name="uq_custom_field_defs_key"),
    CheckConstraint("key ~ '^[a-z][a-z0-9_]{0,63}$'", name="ck_custom_field_defs_key"),
    CheckConstraint("type IN ('text','number','boolean','date','select')", name="ck_custom_field_defs_type"),
)

consent_records = Table(
    "consent_records",
    metadata,
    _id(),
    _tenant(),
    Column("contact_id", UUID(as_uuid=True), nullable=False),
    Column("channel", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("lawful_basis", String(32), nullable=False),
    Column("source", String(64), nullable=False),
    Column("source_ref", String(128), nullable=True),
    Column("captured_at", DateTime(timezone=True), nullable=False),
    Column("policy_version", String(32), nullable=True),
    Column("evidence_ref", String(255), nullable=True),
    Column("recorded_by", String(140), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_consent_records_tenant_id"),
    ForeignKeyConstraint(["tenant_id", "contact_id"], ["contacts.tenant_id", "contacts.id"], name="fk_consent_contact"),
    CheckConstraint("channel IN ('phone','email')", name="ck_consent_channel"),
    CheckConstraint("status IN ('granted','withdrawn')", name="ck_consent_status"),
    CheckConstraint(
        "lawful_basis IN ('consent','legitimate_interest','contract','legal_obligation','vital_interest','public_task')",
        name="ck_consent_lawful_basis",
    ),
    Index("ix_consent_latest", "tenant_id", "contact_id", "channel", "captured_at"),
    comment="append_only: consent evidence history (never edited; withdrawals are new records)",
)

suppressions = Table(
    "suppressions",
    metadata,
    _id(),
    _tenant(),
    Column("channel", String(8), nullable=False),
    Column("value_hash", String(64), nullable=False),  # sha256 of the normalized value
    Column("value", String(320), nullable=True),  # normalized value (null for contact-only suppressions)
    Column("contact_id", UUID(as_uuid=True), nullable=True),
    Column("reason", String(16), nullable=False),
    Column("source_type", String(32), nullable=False),
    Column("source_id", String(128), nullable=True),
    Column("created_by", String(140), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_suppressions_tenant_id"),
    UniqueConstraint("tenant_id", "channel", "value_hash", name="uq_suppressions_value"),
    ForeignKeyConstraint(
        ["tenant_id", "contact_id"],
        ["contacts.tenant_id", "contacts.id"],
        name="fk_suppressions_contact",
    ),
    CheckConstraint("channel IN ('phone','email','all')", name="ck_suppressions_channel"),
    CheckConstraint("reason IN ('opt_out','bounce','complaint','manual','legal')", name="ck_suppressions_reason"),
    CheckConstraint("value_hash ~ '^[0-9a-f]{64}$'", name="ck_suppressions_hash"),
    Index("ix_suppressions_contact", "tenant_id", "contact_id"),
)

contact_notes = Table(
    "contact_notes",
    metadata,
    _id(),
    _tenant(),
    Column("contact_id", UUID(as_uuid=True), nullable=False),
    Column("text", Text, nullable=False),
    Column("source_type", String(32), nullable=False),
    Column("source_id", String(128), nullable=True),
    Column("created_by", String(140), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_contact_notes_tenant_id"),
    ForeignKeyConstraint(
        ["tenant_id", "contact_id"], ["contacts.tenant_id", "contacts.id"], ondelete="CASCADE", name="fk_notes_contact"
    ),
    CheckConstraint("char_length(text) BETWEEN 1 AND 2000", name="ck_contact_notes_length"),
    Index("ix_contact_notes_contact", "tenant_id", "contact_id", "created_at"),
)

imports = Table(
    "imports",
    metadata,
    _id(),
    _tenant(),
    Column("status", String(16), nullable=False, server_default="queued"),
    Column("filename", String(255), nullable=False),
    Column("object_key", String(512), nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("default_region", String(2), nullable=False, server_default="US"),
    Column("default_source", String(64), nullable=True),
    # Consent metadata captured for every imported contact (build.md §11).
    Column("consent", JSONB, nullable=True),
    Column("total_rows", Integer, nullable=False, server_default="0"),
    Column("created_count", Integer, nullable=False, server_default="0"),
    Column("updated_count", Integer, nullable=False, server_default="0"),
    Column("failed_count", Integer, nullable=False, server_default="0"),
    Column("report_key", String(512), nullable=True),
    Column("error_code", String(64), nullable=True),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("created_by", String(140), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_imports_tenant_id"),
    CheckConstraint("status IN ('queued','processing','completed','failed')", name="ck_imports_status"),
)

import_rows = Table(
    "import_rows",
    metadata,
    _id(),
    _tenant(),
    Column("import_id", UUID(as_uuid=True), nullable=False),
    Column("row_number", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("contact_id", UUID(as_uuid=True), nullable=True),
    Column("error", String(200), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_import_rows_tenant_id"),
    UniqueConstraint("tenant_id", "import_id", "row_number", name="uq_import_rows_row"),
    ForeignKeyConstraint(
        ["tenant_id", "import_id"], ["imports.tenant_id", "imports.id"], ondelete="CASCADE", name="fk_rows_import"
    ),
    CheckConstraint("status IN ('created','updated','failed')", name="ck_import_rows_status"),
)

segments = Table(
    "segments",
    metadata,
    _id(),
    _tenant(),
    Column("name", String(200), nullable=False),
    Column("description", String(1000), nullable=True),
    Column("rules", JSONB, nullable=False),
    Column("created_by", String(140), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_segments_tenant_id"),
    UniqueConstraint("tenant_id", "name", name="uq_segments_name"),
)

exports = Table(
    "exports",
    metadata,
    _id(),
    _tenant(),
    Column("kind", String(16), nullable=False, server_default="leads"),
    Column("status", String(16), nullable=False, server_default="queued"),
    Column("filters", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("object_key", String(512), nullable=True),
    Column("row_count", Integer, nullable=True),
    Column("error_code", String(64), nullable=True),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("created_by", String(140), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_exports_tenant_id"),
    CheckConstraint("kind IN ('leads')", name="ck_exports_kind"),
    CheckConstraint("status IN ('queued','processing','completed','failed')", name="ck_exports_status"),
)

crm_jobs = Table(  # ids-only index of background jobs so the sweeper can find stuck ones without tenant context
    "crm_jobs",
    metadata,
    Column("job_id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("kind IN ('import','export')", name="ck_crm_jobs_kind"),
    CheckConstraint("status IN ('queued','processing','done')", name="ck_crm_jobs_status"),
    Index("ix_crm_jobs_open", "status", "updated_at", postgresql_where=text("status <> 'done'")),
)

PLATFORM_SCOPED = {"crm_jobs": "ids-only index of background jobs for the stuck-job sweeper"}
TENANT_TABLES = (
    "organizations",
    "contacts",
    "leads",
    "tags",
    "contact_tags",
    "custom_field_defs",
    "suppressions",
    "contact_notes",
    "imports",
    "import_rows",
    "segments",
    "exports",
)
APPEND_ONLY_TABLES = ("consent_records",)

for _name, _reason in PLATFORM_SCOPED.items():
    metadata.tables[_name].comment = f"platform_scoped: {_reason}"

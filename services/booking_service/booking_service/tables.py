"""SQLAlchemy Core tables owned by Booking Service. Only this service touches them (build.md §3.2).

Tenant-owned (RLS forced): resources, availability_rules, appointments, calendar_sync_state.
Platform-scoped: booking_tenants (ids-only index so periodic jobs can visit each tenant under its own RLS context).

Double booking is impossible at the database level: `ex_appointments_no_overlap` is a GiST exclusion constraint on
(tenant, resource, time range) for every tentative or confirmed appointment (build.md §3.1J, §5.4).
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Table,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID, ExcludeConstraint

from nexus_core.tables import idempotency_table, inbox_table, outbox_table

metadata = MetaData()

outbox = outbox_table(metadata)
inbox = inbox_table(metadata)
idempotency_keys = idempotency_table(metadata)

APPOINTMENT_STATUSES = ("tentative", "confirmed", "canceled", "completed", "no_show")
BLOCKING_STATUSES = ("tentative", "confirmed")


def _ts() -> list[Column]:  # type: ignore[type-arg]
    return [
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ]


# ---------------- platform-scoped ----------------
booking_tenants = Table(
    "booking_tenants",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), primary_key=True),
    Column("first_seen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

# ---------------- tenant-owned (RLS) ----------------
resources = Table(
    "resources",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("name", String(120), nullable=False),
    Column("kind", String(16), nullable=False, server_default="calendar"),
    Column("timezone", String(64), nullable=False, server_default="UTC"),
    Column("status", String(16), nullable=False, server_default="active"),
    # Optional external calendar. The credential is resolved at execution time through integration-service.
    Column("calendar_provider", String(32), nullable=True),
    Column("calendar_id", String(255), nullable=True),
    Column("calendar_connection_id", UUID(as_uuid=True), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_resources_tenant_id"),
    UniqueConstraint("tenant_id", "name", name="uq_resources_name"),
    CheckConstraint("kind IN ('calendar','staff')", name="ck_resources_kind"),
    CheckConstraint("status IN ('active','inactive')", name="ck_resources_status"),
    CheckConstraint(
        "calendar_provider IS NULL OR calendar_provider IN ('google_calendar')", name="ck_resources_calendar_provider"
    ),
    CheckConstraint("(calendar_provider IS NULL) = (calendar_id IS NULL)", name="ck_resources_calendar_pair"),
)

availability_rules = Table(
    "availability_rules",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("resource_id", UUID(as_uuid=True), nullable=False),
    Column("weekday", SmallInteger, nullable=False),  # 0 = Monday … 6 = Sunday (resource timezone)
    Column("start_time", Time, nullable=False),
    Column("end_time", Time, nullable=False),
    Column("slot_minutes", Integer, nullable=False, server_default="30"),
    Column("buffer_before_minutes", Integer, nullable=False, server_default="0"),
    Column("buffer_after_minutes", Integer, nullable=False, server_default="0"),
    Column("min_notice_minutes", Integer, nullable=False, server_default="60"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_availability_rules_tenant_id"),
    ForeignKeyConstraint(
        ["tenant_id", "resource_id"],
        ["resources.tenant_id", "resources.id"],
        ondelete="CASCADE",
        name="fk_availability_rules_resource",
    ),
    CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_rules_weekday"),
    CheckConstraint("end_time > start_time", name="ck_rules_window"),
    CheckConstraint("slot_minutes BETWEEN 5 AND 480", name="ck_rules_slot"),
    CheckConstraint(
        "buffer_before_minutes BETWEEN 0 AND 240 AND buffer_after_minutes BETWEEN 0 AND 240", name="ck_rules_buffers"
    ),
    CheckConstraint("min_notice_minutes BETWEEN 0 AND 20160", name="ck_rules_notice"),
    Index("ix_availability_rules_resource", "tenant_id", "resource_id", "weekday"),
)

appointments = Table(
    "appointments",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("resource_id", UUID(as_uuid=True), nullable=False),
    Column("contact_id", UUID(as_uuid=True), nullable=False),
    Column("start_at", DateTime(timezone=True), nullable=False),
    Column("end_at", DateTime(timezone=True), nullable=False),
    Column("status", String(16), nullable=False),
    Column("source_type", String(32), nullable=False),
    Column("source_id", String(128), nullable=True),
    Column("hold_expires_at", DateTime(timezone=True), nullable=True),
    Column("external_event_id", String(128), nullable=True),
    Column("cancel_reason", String(32), nullable=True),
    Column("confirmed_at", DateTime(timezone=True), nullable=True),
    Column("canceled_at", DateTime(timezone=True), nullable=True),
    Column("reminder_sent_at", DateTime(timezone=True), nullable=True),
    Column("created_by", String(140), nullable=False),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_appointments_tenant_id"),
    ForeignKeyConstraint(
        ["tenant_id", "resource_id"], ["resources.tenant_id", "resources.id"], name="fk_appointments_resource"
    ),
    CheckConstraint(
        "status IN ('tentative','confirmed','canceled','completed','no_show')", name="ck_appointments_status"
    ),
    CheckConstraint("end_at > start_at", name="ck_appointments_range"),
    CheckConstraint("status <> 'tentative' OR hold_expires_at IS NOT NULL", name="ck_appointments_hold"),
    CheckConstraint("source_type IN ('ai_tool','user','api','call','import')", name="ck_appointments_source_type"),
    Index("ix_appointments_range", "tenant_id", "resource_id", "start_at"),
    Index("ix_appointments_contact", "tenant_id", "contact_id", "start_at"),
    Index("ix_appointments_hold", "hold_expires_at", postgresql_where=text("status = 'tentative'")),
)

appointments.append_constraint(
    ExcludeConstraint(
        (appointments.c.tenant_id, "="),
        (appointments.c.resource_id, "="),
        (func.tstzrange(appointments.c.start_at, appointments.c.end_at), "&&"),
        using="gist",
        where=text("status IN ('tentative','confirmed')"),
        name="ex_appointments_no_overlap",
    )
)

calendar_sync_state = Table(
    "calendar_sync_state",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("appointment_id", UUID(as_uuid=True), nullable=False),
    Column("resource_id", UUID(as_uuid=True), nullable=False),
    Column("provider", String(32), nullable=False),
    Column("calendar_id", String(255), nullable=False),
    # Event that should exist externally (deterministic id; NULL means "should not exist").
    Column("desired_event_id", String(128), nullable=True),
    # Event known to exist externally right now, and the calendar it lives in.
    Column("current_event_id", String(128), nullable=True),
    Column("current_calendar_id", String(255), nullable=True),
    Column("state", String(16), nullable=False, server_default="pending"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("lease_until", DateTime(timezone=True), nullable=True),
    Column("last_error", String(64), nullable=True),
    Column("last_synced_at", DateTime(timezone=True), nullable=True),
    *_ts(),
    PrimaryKeyConstraint("tenant_id", "appointment_id"),
    ForeignKeyConstraint(
        ["tenant_id", "appointment_id"],
        ["appointments.tenant_id", "appointments.id"],
        ondelete="CASCADE",
        name="fk_calendar_sync_appointment",
    ),
    CheckConstraint("state IN ('pending','syncing','ambiguous','synced','failed')", name="ck_calendar_sync_state"),
    Index(
        "ix_calendar_sync_due", "next_attempt_at", postgresql_where=text("state IN ('pending','ambiguous','syncing')")
    ),
)

PLATFORM_SCOPED = {"booking_tenants": "ids-only index of tenants with booking data (periodic jobs)"}
TENANT_TABLES = ("resources", "availability_rules", "appointments", "calendar_sync_state")

for _name, _reason in PLATFORM_SCOPED.items():
    metadata.tables[_name].comment = f"platform_scoped: {_reason}"

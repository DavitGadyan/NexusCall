"""Infrastructure tables every service includes in its own metadata/migrations:
outbox (ADR-0008), inbox (ADR-0008) and idempotency keys (build.md §5.4)."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID


def outbox_table(metadata: MetaData) -> Table:
    # Transport table: IDs-only payloads. Platform-scoped so a single relay can drain it;
    # app role may only INSERT, the relay may SELECT/UPDATE (see migration helper).
    return Table(
        "outbox",
        metadata,
        Column("id", BigInteger, primary_key=True, autoincrement=True),
        Column("event_id", UUID(as_uuid=True), nullable=False, unique=True),
        Column("tenant_id", UUID(as_uuid=True), nullable=True),
        Column("event_type", String(128), nullable=False),
        Column("event_version", SmallInteger, nullable=False, server_default=text("1")),
        Column("envelope", JSONB, nullable=False),
        Column("headers", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("published_at", DateTime(timezone=True), nullable=True),
        Column("attempts", Integer, nullable=False, server_default=text("0")),
        Column("last_error", Text, nullable=True),
        Index("ix_outbox_unpublished", "id", postgresql_where=text("published_at IS NULL")),
    )


def inbox_table(metadata: MetaData) -> Table:
    return Table(
        "inbox",
        metadata,
        Column("consumer", String(128), nullable=False),
        Column("event_id", UUID(as_uuid=True), nullable=False),
        Column("tenant_id", UUID(as_uuid=True), nullable=True),
        Column("event_type", String(128), nullable=False),
        Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        PrimaryKeyConstraint("consumer", "event_id"),
    )


def idempotency_table(metadata: MetaData) -> Table:
    return Table(
        "idempotency_keys",
        metadata,
        Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
        Column("tenant_id", UUID(as_uuid=True), nullable=False),
        Column("operation", String(128), nullable=False),
        Column("key", String(255), nullable=False),
        Column("request_hash", String(64), nullable=False),
        Column("state", String(32), nullable=False),  # processing | succeeded | failed_retryable
        Column("response_status", Integer, nullable=True),
        Column("response_body", JSONB, nullable=True),
        Column("resource_id", String(128), nullable=True),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column(
            "expires_at", DateTime(timezone=True), nullable=False, server_default=text("now() + interval '24 hours'")
        ),
        UniqueConstraint("tenant_id", "operation", "key", name="uq_idempotency_scope"),
    )


def create_infra_tables(op: object, app_role: str, relay_role: str | None = None) -> None:
    """Alembic helper: grants and RLS for the infra tables (tables created via metadata)."""
    from nexus_core.rls import apply_tenant_rls, mark_platform_scoped

    o = op  # alembic.op
    mark_platform_scoped(o, "outbox", "transport table, ids-only payloads, drained by relay")
    mark_platform_scoped(o, "inbox", "consumer dedupe table, ids only")
    o.execute(f"GRANT INSERT, SELECT, UPDATE ON outbox TO {app_role}")  # type: ignore[attr-defined]
    o.execute(f"GRANT USAGE, SELECT ON SEQUENCE outbox_id_seq TO {app_role}")  # type: ignore[attr-defined]
    o.execute(f"GRANT INSERT, SELECT ON inbox TO {app_role}")  # type: ignore[attr-defined]
    apply_tenant_rls(o, "idempotency_keys", app_role)
    if relay_role:
        o.execute(f"GRANT SELECT, UPDATE ON outbox TO {relay_role}")  # type: ignore[attr-defined]

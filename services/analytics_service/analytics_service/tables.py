"""SQLAlchemy Core tables owned by Analytics & Reporting Service. Only this service touches them (build.md §3.2).

Event-fed read models (build.md §3.1O): no cross-service joins, tenant-owned, forced RLS.
* `fact_*`: one row per business object, updated from its events (out-of-order tolerant).
* `daily_metrics`: (tenant, day, metric, dimension) counters. Increments happen only on a fact's
  first-seen transition, so redelivered events never double-count.
* `exports`: async export jobs (`analytics.aggregate` queue) whose CSV lands in the `exports` bucket.

Opens and clicks are deliberately not modelled: they are unreliable signals (build.md §3.1K).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
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
from sqlalchemy.dialects.postgresql import JSONB, UUID

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


def _false(name: str) -> Column:  # type: ignore[type-arg]
    return Column(name, Boolean, nullable=False, server_default=text("false"))


fact_calls = Table(
    "fact_calls",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),  # call id
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("campaign_id", UUID(as_uuid=True), nullable=True),
    Column("contact_id", UUID(as_uuid=True), nullable=True),
    Column("direction", String(16), nullable=True),
    Column("state", String(32), nullable=True),
    Column("state_at", DateTime(timezone=True), nullable=True),
    Column("final_state", String(32), nullable=True),
    _false("answered"),
    _false("completed"),
    _false("opted_out"),
    Column("duration_seconds", Integer, nullable=False, server_default=text("0")),
    Column("cost_micros", BigInteger, nullable=False, server_default=text("0")),
    Column("outcome", String(64), nullable=True),
    Column("qualified", Boolean, nullable=True),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_fact_calls_tenant_id"),
    Index("ix_fact_calls_campaign", "tenant_id", "campaign_id"),
)

fact_emails = Table(
    "fact_emails",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),  # email-service message id
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("campaign_id", UUID(as_uuid=True), nullable=True),
    Column("category", String(16), nullable=True),
    Column("state", String(32), nullable=True),
    Column("state_at", DateTime(timezone=True), nullable=True),
    _false("accepted"),
    _false("delivered"),
    _false("bounced"),
    _false("complained"),
    _false("unsubscribed"),
    _false("rejected"),
    _false("failed"),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_fact_emails_tenant_id"),
    Index("ix_fact_emails_campaign", "tenant_id", "campaign_id"),
)

fact_bookings = Table(
    "fact_bookings",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),  # booking id
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("contact_id", UUID(as_uuid=True), nullable=True),
    Column("status", String(16), nullable=True),
    _false("confirmed"),
    _false("canceled"),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_fact_bookings_tenant_id"),
)

fact_ai_usage = Table(
    "fact_ai_usage",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),  # ai-gateway request id
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("model", String(64), nullable=False),
    Column("provider", String(32), nullable=False),
    Column("purpose", String(32), nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cost_micros", BigInteger, nullable=False),
    Column("latency_ms", Integer, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_fact_ai_usage_tenant_id"),
    CheckConstraint("input_tokens >= 0 AND output_tokens >= 0 AND cost_micros >= 0", name="ck_fact_ai_usage_nonneg"),
)

fact_drafts = Table(
    "fact_drafts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),  # draft id
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("thread_id", UUID(as_uuid=True), nullable=True),
    Column("status", String(16), nullable=True),
    Column("generated_version", Integer, nullable=True),
    Column("approved_version", Integer, nullable=True),
    _false("generated"),
    _false("approved"),
    _false("edited"),
    _false("rejected"),
    _false("sent"),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_fact_drafts_tenant_id"),
)

fact_leads = Table(
    "fact_leads",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),  # contact id
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("lead_id", UUID(as_uuid=True), nullable=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_fact_leads_tenant_id"),
)

fact_campaigns = Table(
    "fact_campaigns",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),  # campaign id
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("channel", String(8), nullable=False),
    Column("launched_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_fact_campaigns_tenant_id"),
)

daily_metrics = Table(
    "daily_metrics",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("day", Date, nullable=False),
    Column("metric", String(64), nullable=False),
    Column("dimension", String(128), nullable=False, server_default=""),
    Column("value", BigInteger, nullable=False, server_default=text("0")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("tenant_id", "day", "metric", "dimension"),
    Index("ix_daily_metrics_metric", "tenant_id", "metric", "day"),
)

exports = Table(
    "exports",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("params", JSONB, nullable=False),
    Column("status", String(16), nullable=False, server_default="queued"),
    Column("requested_by", String(128), nullable=False),
    Column("object_key", String(255), nullable=True),
    Column("row_count", Integer, nullable=True),
    Column("error_code", String(64), nullable=True),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_exports_tenant_id"),
    CheckConstraint("status IN ('queued','processing','succeeded','failed')", name="ck_exports_status"),
    CheckConstraint("kind IN ('daily_metrics')", name="ck_exports_kind"),
    Index("ix_exports_tenant_created", "tenant_id", "created_at"),
    Index("ix_exports_open", "status", "updated_at", postgresql_where=text("status IN ('queued','processing')")),
)

PLATFORM_SCOPED: dict[str, str] = {}
TENANT_TABLES = (
    "fact_calls",
    "fact_emails",
    "fact_bookings",
    "fact_ai_usage",
    "fact_drafts",
    "fact_leads",
    "fact_campaigns",
    "daily_metrics",
    "exports",
)

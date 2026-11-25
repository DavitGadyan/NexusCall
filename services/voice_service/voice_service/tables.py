"""SQLAlchemy Core tables owned by the Voice / Conversation Service. Only this service touches them (build.md §3.2).

Tenant-owned (forced RLS): voice_sessions, transcript_segments, recordings, handoffs.
Platform-scoped, ids only: session_directory (work index for the post-processing sweeper and retention job,
which must find sessions across tenants before opening each tenant's session).
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
from sqlalchemy.dialects.postgresql import UUID

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


voice_sessions = Table(
    "voice_sessions",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("call_id", UUID(as_uuid=True), nullable=False),
    Column("agent_id", UUID(as_uuid=True), nullable=True),
    Column("contact_id", UUID(as_uuid=True), nullable=True),
    Column("direction", String(8), nullable=False),
    Column("stream_sid", String(64), nullable=True),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("end_reason", String(32), nullable=True),
    Column("turns", Integer, nullable=False, server_default="0"),
    Column("barge_ins", Integer, nullable=False, server_default="0"),
    Column("opted_out", Boolean, nullable=False, server_default=text("false")),
    Column("handoff_requested", Boolean, nullable=False, server_default=text("false")),
    Column("recording_enabled", Boolean, nullable=False, server_default=text("false")),
    Column("disclosure_played", Boolean, nullable=False, server_default=text("false")),
    Column("consent_prompt_played", Boolean, nullable=False, server_default=text("false")),
    Column("agent_version", Integer, nullable=True),
    Column("transcript_key", String(255), nullable=True),
    Column("transcript_purged_at", DateTime(timezone=True), nullable=True),
    Column("postprocess_status", String(16), nullable=False, server_default="pending"),
    Column("postprocess_attempts", Integer, nullable=False, server_default="0"),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("ended_at", DateTime(timezone=True), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_voice_sessions_tenant_id"),
    UniqueConstraint("tenant_id", "call_id", name="uq_voice_sessions_call"),
    CheckConstraint("direction IN ('inbound','outbound')", name="ck_voice_sessions_direction"),
    CheckConstraint("status IN ('active','ended','failed')", name="ck_voice_sessions_status"),
    CheckConstraint(
        "postprocess_status IN ('pending','queued','done','failed','skipped')", name="ck_voice_sessions_postprocess"
    ),
)

transcript_segments = Table(
    "transcript_segments",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("session_id", UUID(as_uuid=True), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("role", String(8), nullable=False),
    # Restricted data (build.md §11): readable only with calls:recordings:read; purged by the retention job.
    Column("text", Text, nullable=False),
    Column("started_ms", Integer, nullable=False, server_default="0"),
    Column("interrupted", Boolean, nullable=False, server_default=text("false")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_transcript_segments_tenant_id"),
    UniqueConstraint("tenant_id", "session_id", "seq", name="uq_transcript_segments_seq"),
    ForeignKeyConstraint(
        ["tenant_id", "session_id"],
        ["voice_sessions.tenant_id", "voice_sessions.id"],
        ondelete="CASCADE",
        name="fk_transcript_segments_session",
    ),
    CheckConstraint("role IN ('agent','caller')", name="ck_transcript_segments_role"),
    CheckConstraint("length(text) <= 4000", name="ck_transcript_segments_len"),
)

recordings = Table(
    "recordings",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("session_id", UUID(as_uuid=True), nullable=False),
    Column("call_id", UUID(as_uuid=True), nullable=False),
    Column("bucket", String(32), nullable=False),
    Column("object_key", String(255), nullable=False),
    Column("content_type", String(64), nullable=False),
    Column("channels", Integer, nullable=False, server_default="2"),
    Column("duration_seconds", Integer, nullable=False, server_default="0"),
    Column("size_bytes", Integer, nullable=False, server_default="0"),
    Column("status", String(16), nullable=False, server_default="stored"),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_recordings_tenant_id"),
    UniqueConstraint("tenant_id", "session_id", name="uq_recordings_session"),
    ForeignKeyConstraint(
        ["tenant_id", "session_id"],
        ["voice_sessions.tenant_id", "voice_sessions.id"],
        ondelete="CASCADE",
        name="fk_recordings_session",
    ),
    CheckConstraint("status IN ('stored','deleted')", name="ck_recordings_status"),
    Index("ix_recordings_call", "tenant_id", "call_id"),
)

handoffs = Table(
    "handoffs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("session_id", UUID(as_uuid=True), nullable=False),
    Column("call_id", UUID(as_uuid=True), nullable=False),
    Column("trigger", String(16), nullable=False),
    Column("reason", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_handoffs_tenant_id"),
    ForeignKeyConstraint(
        ["tenant_id", "session_id"],
        ["voice_sessions.tenant_id", "voice_sessions.id"],
        ondelete="CASCADE",
        name="fk_handoffs_session",
    ),
    CheckConstraint("trigger IN ('caller_request','agent_tool')", name="ck_handoffs_trigger"),
    CheckConstraint("status IN ('requested','transferring','unavailable','failed')", name="ck_handoffs_status"),
)

session_directory = Table(
    "session_directory",
    metadata,
    Column("session_id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("ended_at", DateTime(timezone=True), nullable=True),
    Column("postprocess_due_at", DateTime(timezone=True), nullable=True),
    Column("retention_done", Boolean, nullable=False, server_default=text("false")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index(
        "ix_session_directory_postprocess",
        "postprocess_due_at",
        postgresql_where=text("postprocess_due_at IS NOT NULL"),
    ),
    Index("ix_session_directory_retention", "tenant_id", postgresql_where=text("NOT retention_done")),
)

PLATFORM_SCOPED = {
    "session_directory": "ids-only session -> tenant work index for the post-processing sweeper and retention job",
}
TENANT_TABLES = ("voice_sessions", "transcript_segments", "recordings", "handoffs")

for _name, _reason in PLATFORM_SCOPED.items():
    metadata.tables[_name].comment = f"platform_scoped: {_reason}"

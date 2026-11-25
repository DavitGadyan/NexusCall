"""SQLAlchemy Core tables owned by AI Agent Service. Only this service touches them (build.md §3.2).

Tenant-owned (forced RLS): agents, prompt_versions (append-only), knowledge_sources, knowledge_documents,
knowledge_chunks, knowledge_object_tombstones, eval_datasets, eval_cases, eval_runs, drafting_runs
(append-only), call_postprocess_runs.
Platform-scoped: agent_jobs (ids-only index for the stuck-job sweeper).

No table stores prompt or completion text produced at runtime (OWASP LLM02). Prompt *configuration* written
by tenant admins and knowledge-chunk text are stored because they are the product's configuration and corpus.
"""

from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from nexus_core.tables import idempotency_table, inbox_table, outbox_table

EMBEDDING_DIMS = 1024

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


CLASSIFICATIONS = "('public','internal','confidential','restricted')"

# ---------------------------------------------------------------------------------------------- agents
agents = Table(
    "agents",
    metadata,
    _id(),
    _tenant(),
    Column("name", String(120), nullable=False),
    Column("description", String(500), nullable=False, server_default=""),
    Column("kind", String(16), nullable=False),
    Column("status", String(16), nullable=False, server_default="draft"),
    Column("current_version", Integer, nullable=True),
    Column("latest_version", Integer, nullable=False, server_default="0"),
    Column("version", Integer, nullable=False, server_default="1"),  # optimistic lock
    Column("created_by", String(160), nullable=True),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_agents_tenant_id"),
    UniqueConstraint("tenant_id", "name", name="uq_agents_name"),
    CheckConstraint("kind IN ('voice','email')", name="ck_agents_kind"),
    CheckConstraint("status IN ('draft','active','archived')", name="ck_agents_status"),
    CheckConstraint("status <> 'active' OR current_version IS NOT NULL", name="ck_agents_active_has_version"),
)

prompt_versions = Table(
    "prompt_versions",
    metadata,
    _id(),
    _tenant(),
    Column("agent_id", UUID(as_uuid=True), nullable=False),
    Column("version", Integer, nullable=False),
    Column("system_prompt", Text, nullable=False),
    Column("greeting", Text, nullable=False, server_default=""),
    Column("disclosure_text", Text, nullable=False, server_default=""),
    Column("model", String(80), nullable=True),
    Column("voice_id", String(120), nullable=True),
    # Recorded configuration only: the gateway API takes no sampling parameters (API version 2026-10-21).
    Column("temperature", Float, nullable=True),
    Column("tool_ids", ARRAY(String(83)), nullable=False, server_default=text("'{}'")),
    Column("knowledge_source_ids", ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'")),
    Column("escalation_rules", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("max_call_seconds", Integer, nullable=False, server_default="600"),
    Column("change_note", String(500), nullable=False, server_default=""),
    Column("created_by", String(160), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_prompt_versions_tenant_id"),
    UniqueConstraint("tenant_id", "agent_id", "version", name="uq_prompt_versions_agent_version"),
    ForeignKeyConstraint(["tenant_id", "agent_id"], ["agents.tenant_id", "agents.id"], name="fk_prompt_versions_agent"),
    CheckConstraint("version >= 1", name="ck_prompt_versions_version"),
    CheckConstraint("max_call_seconds BETWEEN 30 AND 3600", name="ck_prompt_versions_max_call"),
    comment="append_only: immutable prompt and agent configuration history",
)

# ---------------------------------------------------------------------------------------------- knowledge
knowledge_sources = Table(
    "knowledge_sources",
    metadata,
    _id(),
    _tenant(),
    Column("name", String(120), nullable=False),
    Column("description", String(500), nullable=False, server_default=""),
    Column("origin", String(16), nullable=False, server_default="internal"),
    Column("classification", String(16), nullable=False, server_default="internal"),
    # Document-level ACL (OWASP LLM09). Empty = every member with ai:agents:read; agents never see restricted-role
    # sources because they act for an external party.
    Column("allowed_roles", ARRAY(String(64)), nullable=False, server_default=text("'{}'")),
    # High-risk sources: every document needs a human approval before its chunks become retrievable (LLM05).
    Column("requires_approval", Boolean, nullable=False, server_default=text("false")),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("created_by", String(160), nullable=True),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_knowledge_sources_tenant_id"),
    UniqueConstraint("tenant_id", "name", name="uq_knowledge_sources_name"),
    CheckConstraint("origin IN ('internal','external')", name="ck_knowledge_sources_origin"),
    CheckConstraint(f"classification IN {CLASSIFICATIONS}", name="ck_knowledge_sources_classification"),
    CheckConstraint("status IN ('active','disabled')", name="ck_knowledge_sources_status"),
)

knowledge_documents = Table(
    "knowledge_documents",
    metadata,
    _id(),
    _tenant(),
    Column("source_id", UUID(as_uuid=True), nullable=False),
    Column("filename", String(255), nullable=False),
    Column("content_type", String(64), nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("object_key", String(512), nullable=True),
    Column("doc_version", Integer, nullable=False, server_default="1"),
    Column("uploaded_by", String(160), nullable=True),
    Column("status", String(16), nullable=False, server_default="queued"),
    Column("review_state", String(24), nullable=False, server_default="pending_analysis"),
    Column("review_reasons", ARRAY(String(64)), nullable=False, server_default=text("'{}'")),
    Column("scan_status", String(16), nullable=False, server_default="pending"),
    Column("pii_detected", Boolean, nullable=False, server_default=text("false")),
    Column("chunk_count", Integer, nullable=False, server_default="0"),
    Column("char_count", Integer, nullable=False, server_default="0"),
    Column("error_code", String(64), nullable=True),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("reviewed_by", String(160), nullable=True),
    Column("reviewed_at", DateTime(timezone=True), nullable=True),
    Column("indexed_at", DateTime(timezone=True), nullable=True),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_knowledge_documents_tenant_id"),
    UniqueConstraint("tenant_id", "source_id", "sha256", name="uq_knowledge_documents_sha"),
    ForeignKeyConstraint(
        ["tenant_id", "source_id"],
        ["knowledge_sources.tenant_id", "knowledge_sources.id"],
        ondelete="CASCADE",
        name="fk_knowledge_documents_source",
    ),
    CheckConstraint("status IN ('queued','processing','indexed','failed')", name="ck_knowledge_documents_status"),
    CheckConstraint(
        "review_state IN ('pending_analysis','not_required','pending_approval','needs_review','approved','rejected')",
        name="ck_knowledge_documents_review",
    ),
    CheckConstraint("scan_status IN ('pending','clean','suspicious','skipped')", name="ck_knowledge_documents_scan"),
    CheckConstraint("size_bytes >= 0 AND size_bytes <= 10485760", name="ck_knowledge_documents_size"),
)

knowledge_chunks = Table(
    "knowledge_chunks",
    metadata,
    _id(),
    _tenant(),
    Column("source_id", UUID(as_uuid=True), nullable=False),
    Column("document_id", UUID(as_uuid=True), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("text", Text, nullable=False),
    Column("content_sha256", String(64), nullable=False),
    # NULL until embedded; a model change re-embeds every chunk (searches never mix models, LLM09).
    Column("embedding", Vector(EMBEDDING_DIMS), nullable=True),
    Column("embedding_model", String(80), nullable=True),
    Column("embedded_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_knowledge_chunks_tenant_id"),
    UniqueConstraint("tenant_id", "document_id", "ordinal", name="uq_knowledge_chunks_ordinal"),
    ForeignKeyConstraint(
        ["tenant_id", "document_id"],
        ["knowledge_documents.tenant_id", "knowledge_documents.id"],
        ondelete="CASCADE",
        name="fk_knowledge_chunks_document",
    ),
    # Exact KNN inside one tenant's partition. There is deliberately no global ANN index: an approximate index
    # would rank every tenant's vectors before the RLS filter and leak through result counts (LLM09).
    Index("ix_knowledge_chunks_tenant_model", "tenant_id", "embedding_model"),
)

knowledge_object_tombstones = Table(
    "knowledge_object_tombstones",
    metadata,
    _id(),
    _tenant(),
    Column("bucket", String(32), nullable=False),
    Column("object_key", String(512), nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_knowledge_tombstones_tenant_id"),
)

# ---------------------------------------------------------------------------------------------- evaluations
eval_datasets = Table(
    "eval_datasets",
    metadata,
    _id(),
    _tenant(),
    Column("name", String(120), nullable=False),
    Column("description", String(500), nullable=False, server_default=""),
    Column("created_by", String(160), nullable=True),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_eval_datasets_tenant_id"),
    UniqueConstraint("tenant_id", "name", name="uq_eval_datasets_name"),
)

eval_cases = Table(
    "eval_cases",
    metadata,
    _id(),
    _tenant(),
    Column("dataset_id", UUID(as_uuid=True), nullable=False),
    Column("name", String(120), nullable=False),
    Column("kind", String(16), nullable=False, server_default="quality"),
    Column("source", String(16), nullable=False, server_default="email"),
    Column("input_text", Text, nullable=False),
    Column("knowledge_text", Text, nullable=True),
    Column("expectations", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_by", String(160), nullable=True),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_eval_cases_tenant_id"),
    ForeignKeyConstraint(
        ["tenant_id", "dataset_id"],
        ["eval_datasets.tenant_id", "eval_datasets.id"],
        ondelete="CASCADE",
        name="fk_eval_cases_dataset",
    ),
    CheckConstraint("kind IN ('quality','injection','poisoning')", name="ck_eval_cases_kind"),
    CheckConstraint("source IN ('email','transcript','document','knowledge','user')", name="ck_eval_cases_source"),
)

eval_runs = Table(
    "eval_runs",
    metadata,
    _id(),
    _tenant(),
    Column("dataset_id", UUID(as_uuid=True), nullable=False),
    Column("agent_id", UUID(as_uuid=True), nullable=False),
    Column("prompt_version", Integer, nullable=False),
    Column("include_security_cases", Boolean, nullable=False, server_default=text("true")),
    Column("status", String(16), nullable=False, server_default="queued"),
    Column("case_count", Integer, nullable=False, server_default="0"),
    Column("passed", Integer, nullable=False, server_default="0"),
    Column("failed", Integer, nullable=False, server_default="0"),
    # Per case: id, kind, passed, failed checks, gateway flags, tool statuses. Never model text (LLM02).
    Column("results", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("error_code", String(64), nullable=True),
    Column("created_by", String(160), nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    *_ts(),
    UniqueConstraint("tenant_id", "id", name="uq_eval_runs_tenant_id"),
    ForeignKeyConstraint(
        ["tenant_id", "dataset_id"],
        ["eval_datasets.tenant_id", "eval_datasets.id"],
        ondelete="CASCADE",
        name="fk_eval_runs_dataset",
    ),
    ForeignKeyConstraint(["tenant_id", "agent_id"], ["agents.tenant_id", "agents.id"], name="fk_eval_runs_agent"),
    CheckConstraint("status IN ('queued','running','completed','failed')", name="ck_eval_runs_status"),
)

# ---------------------------------------------------------------------------------------------- provenance
drafting_runs = Table(
    "drafting_runs",
    metadata,
    _id(),
    _tenant(),
    Column("thread_id", UUID(as_uuid=True), nullable=False),
    Column("message_id", UUID(as_uuid=True), nullable=False),
    Column("agent_id", UUID(as_uuid=True), nullable=False),
    Column("prompt_version", Integer, nullable=False),
    Column("intent", String(32), nullable=False),
    Column("priority", String(16), nullable=False),
    Column("sensitive", Boolean, nullable=False),
    Column("model", String(80), nullable=False),
    Column("provider", String(32), nullable=False),
    Column("gateway_request_ids", ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'")),
    Column("retrieved_chunk_ids", ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'")),
    Column("cited_chunk_ids", ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'")),
    Column("quality_flags", ARRAY(String(64)), nullable=False, server_default=text("'{}'")),
    Column("confidence", Float, nullable=False),
    Column("input_tokens", Integer, nullable=False, server_default="0"),
    Column("output_tokens", Integer, nullable=False, server_default="0"),
    Column("cost_micros", Integer, nullable=False, server_default="0"),
    Column("latency_ms", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_drafting_runs_tenant_id"),
    Index("ix_drafting_runs_thread", "tenant_id", "thread_id"),
    comment="append_only: drafting provenance (ids, enums and metrics only, never email or draft text)",
)

call_postprocess_runs = Table(
    "call_postprocess_runs",
    metadata,
    _id(),
    _tenant(),
    Column("call_id", UUID(as_uuid=True), nullable=False),
    Column("agent_id", UUID(as_uuid=True), nullable=False),
    Column("contact_id", UUID(as_uuid=True), nullable=True),
    Column("outcome", String(32), nullable=False),
    Column("qualified", Boolean, nullable=True),
    Column("follow_up_needed", Boolean, nullable=False, server_default=text("false")),
    Column("gateway_request_id", UUID(as_uuid=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "id", name="uq_call_postprocess_tenant_id"),
    UniqueConstraint("tenant_id", "call_id", name="uq_call_postprocess_call"),
)

# ---------------------------------------------------------------------------------------------- platform-scoped
agent_jobs = Table(
    "agent_jobs",
    metadata,
    Column("job_id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("status", String(16), nullable=False, server_default="queued"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("kind IN ('ingest','reembed','eval','object_delete')", name="ck_agent_jobs_kind"),
    CheckConstraint("status IN ('queued','done')", name="ck_agent_jobs_status"),
    Index("ix_agent_jobs_pending", "updated_at", postgresql_where=text("status <> 'done'")),
)

PLATFORM_SCOPED = {"agent_jobs": "ids-only index of background jobs for the stuck-job sweeper"}
TENANT_TABLES = (
    "agents",
    "knowledge_sources",
    "knowledge_documents",
    "knowledge_chunks",
    "knowledge_object_tombstones",
    "eval_datasets",
    "eval_cases",
    "eval_runs",
    "call_postprocess_runs",
)
APPEND_ONLY_TABLES = ("prompt_versions", "drafting_runs")

for _name, _reason in PLATFORM_SCOPED.items():
    metadata.tables[_name].comment = f"platform_scoped: {_reason}"

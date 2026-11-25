"""Request and response models. Requests forbid unknown fields, so a client can never set server-owned state
such as `approved`, `review_state`, `embedding_model` or `uploaded_by` (OWASP LLM05)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_agent_service import domain

Classification = Literal["public", "internal", "confidential", "restricted"]
AgentKind = Literal["voice", "email"]
_TOOL_ID = re.compile(r"^[a-z][a-z0-9_]{1,40}\.[a-z][a-z0-9_]{1,40}$")
_ROLE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def _check_roles(v: list[str]) -> list[str]:
    if any(not _ROLE.match(r) for r in v):
        raise ValueError("invalid role key")
    return sorted(set(v))


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------------------------------- agents
class EscalationRule(_In):
    trigger: Literal["caller_request", "sensitive_topic", "complaint", "legal", "repeated_failure", "keyword"]
    action: Literal["transfer_to_human", "end_call", "flag_for_review"]
    keyword: str | None = Field(default=None, max_length=60)


class VersionIn(_In):
    system_prompt: str = Field(min_length=20, max_length=20_000)
    greeting: str = Field(default="", max_length=1000)
    disclosure_text: str = Field(default="", max_length=1000)
    model: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9.-]{2,79}$")
    voice_id: str | None = Field(default=None, max_length=120, pattern=r"^[A-Za-z0-9_.-]+$")
    temperature: float | None = Field(default=None, ge=0, le=1)
    tool_ids: list[str] = Field(default_factory=list, max_length=12)
    knowledge_source_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    escalation_rules: list[EscalationRule] = Field(default_factory=list, max_length=20)
    max_call_seconds: int = Field(default=600, ge=30, le=3600)
    change_note: str = Field(default="", max_length=500)

    @field_validator("tool_ids")
    @classmethod
    def _tools(cls, v: list[str]) -> list[str]:
        if any(not _TOOL_ID.match(t) for t in v):
            raise ValueError("invalid tool id")
        return sorted(set(v))

    @field_validator("system_prompt", "greeting", "disclosure_text")
    @classmethod
    def _no_secrets(cls, v: str) -> str:
        if domain.contains_credentials(v):
            raise ValueError("prompts must not contain credentials or secrets")
        return v


class AgentCreate(_In):
    name: str = Field(min_length=2, max_length=120)
    kind: AgentKind
    description: str = Field(default="", max_length=500)
    config: VersionIn


class AgentPatch(_In):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    version: int = Field(ge=1, description="Current row version (optimistic locking)")


class AgentOut(_Out):
    id: uuid.UUID
    name: str
    description: str
    kind: str
    status: str
    current_version: int | None
    latest_version: int
    version: int
    created_at: datetime
    updated_at: datetime


class VersionOut(_Out):
    id: uuid.UUID
    agent_id: uuid.UUID
    version: int
    system_prompt: str
    greeting: str
    disclosure_text: str
    model: str | None
    voice_id: str | None
    temperature: float | None
    tool_ids: list[str]
    knowledge_source_ids: list[uuid.UUID]
    escalation_rules: list[dict[str, Any]]
    max_call_seconds: int
    change_note: str
    created_by: str | None
    created_at: datetime


class AgentPage(BaseModel):
    items: list[AgentOut]
    next_cursor: str | None


class VersionPage(BaseModel):
    items: list[VersionOut]
    next_cursor: str | None


# ------------------------------------------------------------------------------------------- knowledge
class SourceCreate(_In):
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=500)
    origin: Literal["internal", "external"] = "internal"
    classification: Classification = "internal"
    allowed_roles: list[str] = Field(default_factory=list, max_length=10)
    requires_approval: bool = False

    @field_validator("allowed_roles")
    @classmethod
    def _roles(cls, v: list[str]) -> list[str]:
        return _check_roles(v)


class SourcePatch(_In):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    classification: Classification | None = None
    allowed_roles: list[str] | None = Field(default=None, max_length=10)
    requires_approval: bool | None = None
    status: Literal["active", "disabled"] | None = None
    version: int = Field(ge=1)

    @field_validator("allowed_roles")
    @classmethod
    def _roles(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _check_roles(v)


class SourceOut(_Out):
    id: uuid.UUID
    name: str
    description: str
    origin: str
    classification: str
    allowed_roles: list[str]
    requires_approval: bool
    status: str
    version: int
    created_at: datetime
    updated_at: datetime


class SourcePage(BaseModel):
    items: list[SourceOut]
    next_cursor: str | None


class DocumentOut(_Out):
    id: uuid.UUID
    source_id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    doc_version: int
    uploaded_by: str | None
    status: str
    review_state: str
    review_reasons: list[str]
    scan_status: str
    pii_detected: bool
    chunk_count: int
    error_code: str | None
    reviewed_by: str | None
    reviewed_at: datetime | None
    indexed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DocumentPage(BaseModel):
    items: list[DocumentOut]
    next_cursor: str | None


class ReviewIn(_In):
    decision: Literal["approve", "reject"]
    note: str = Field(default="", max_length=500)


class DeleteOut(BaseModel):
    deleted: bool
    chunks_deleted: int
    documents_deleted: int = 0


class SearchIn(_In):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=4, ge=1, le=8)
    source_ids: list[uuid.UUID] | None = Field(default=None, max_length=20)


class SearchResultOut(BaseModel):
    rank: int
    chunk_id: uuid.UUID
    source_id: uuid.UUID
    source_name: str
    document_id: uuid.UUID
    text: str
    # Raw similarity only for knowledge managers (tuning). Ordinary readers get the rank (LLM09).
    similarity: float | None = None


class SearchOut(BaseModel):
    results: list[SearchResultOut]
    embedding_model: str


class ReembedOut(BaseModel):
    job_id: uuid.UUID | None
    stale_chunks: int
    embedding_model: str


# ------------------------------------------------------------------------------------------- evaluations
class Expectations(_In):
    forbidden_phrases: list[str] = Field(default_factory=list, max_length=20)
    required_phrases: list[str] = Field(default_factory=list, max_length=20)
    expect_flags: list[Literal["prompt_injection_suspected", "input_secret_redacted", "output_blocked"]] = Field(
        default_factory=list, max_length=5
    )
    check_system_prompt_leak: bool = False

    @field_validator("forbidden_phrases", "required_phrases")
    @classmethod
    def _phrases(cls, v: list[str]) -> list[str]:
        if any(not p.strip() or len(p) > 200 for p in v):
            raise ValueError("phrases must be 1..200 characters")
        return v


class DatasetCreate(_In):
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=500)


class DatasetOut(_Out):
    id: uuid.UUID
    name: str
    description: str
    created_at: datetime
    updated_at: datetime


class DatasetPage(BaseModel):
    items: list[DatasetOut]
    next_cursor: str | None


class CaseCreate(_In):
    name: str = Field(min_length=2, max_length=120)
    kind: Literal["quality", "injection", "poisoning"] = "quality"
    source: Literal["email", "transcript", "document", "knowledge", "user"] = "email"
    input_text: str = Field(min_length=1, max_length=20_000)
    knowledge_text: str | None = Field(default=None, max_length=20_000)
    expectations: Expectations = Field(default_factory=Expectations)


class CaseOut(_Out):
    id: uuid.UUID
    dataset_id: uuid.UUID
    name: str
    kind: str
    source: str
    input_text: str
    knowledge_text: str | None
    expectations: dict[str, Any]
    created_at: datetime


class CasePage(BaseModel):
    items: list[CaseOut]
    next_cursor: str | None


class RunCreate(_In):
    agent_id: uuid.UUID
    prompt_version: int | None = Field(default=None, ge=1)
    include_security_cases: bool = True


class RunOut(_Out):
    id: uuid.UUID
    dataset_id: uuid.UUID
    agent_id: uuid.UUID
    prompt_version: int
    include_security_cases: bool
    status: str
    case_count: int
    passed: int
    failed: int
    results: list[dict[str, Any]]
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunPage(BaseModel):
    items: list[RunOut]
    next_cursor: str | None


class DraftingRunOut(_Out):
    id: uuid.UUID
    thread_id: uuid.UUID
    message_id: uuid.UUID
    agent_id: uuid.UUID
    prompt_version: int
    intent: str
    priority: str
    sensitive: bool
    model: str
    provider: str
    retrieved_chunk_ids: list[uuid.UUID]
    cited_chunk_ids: list[uuid.UUID]
    quality_flags: list[str]
    confidence: float
    input_tokens: int
    output_tokens: int
    cost_micros: int
    latency_ms: int
    created_at: datetime


class DraftingRunPage(BaseModel):
    items: list[DraftingRunOut]
    next_cursor: str | None


# ------------------------------------------------------------------------------------------- internal contracts (§5)
class InternalAgentOut(BaseModel):
    agent_id: uuid.UUID
    name: str
    kind: str
    status: str


class RuntimeConfigOut(BaseModel):
    agent_id: uuid.UUID
    version: int
    kind: str
    system_prompt: str
    model: str | None
    voice_id: str | None
    tool_ids: list[str]
    greeting: str
    disclosure_text: str
    escalation_rules: list[dict[str, Any]]
    max_call_seconds: int


class InternalSearchIn(_In):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=4, ge=1, le=8)
    source_ids: list[uuid.UUID] | None = Field(default=None, max_length=20)


class InternalChunk(BaseModel):
    chunk_id: uuid.UUID
    source_id: uuid.UUID
    source_name: str
    text: str
    # Rank-derived (1.0 = best), never the raw similarity: tool output reaches the model and the customer.
    score: float


class InternalSearchOut(BaseModel):
    chunks: list[InternalChunk]


class ContextBlockIn(_In):
    text: str = Field(min_length=0, max_length=50_000)
    source: Literal["email", "attachment", "crm", "user", "transcript", "document"] = "email"
    trust: Literal["trusted", "untrusted"] = "untrusted"  # accepted for the contract; always treated as untrusted


class DraftingIn(_In):
    thread_id: uuid.UUID
    message_id: uuid.UUID
    agent_id: uuid.UUID | None = None
    context_blocks: list[ContextBlockIn] = Field(min_length=1, max_length=40)


class CitationOut(BaseModel):
    chunk_id: uuid.UUID
    source_id: uuid.UUID
    source_name: str


class QualityOut(BaseModel):
    confidence: float
    flags: list[str]


class DraftingOut(BaseModel):
    run_id: uuid.UUID
    intent: str
    priority: str
    sensitive: bool
    summary: str
    draft_text: str
    citations: list[CitationOut]
    quality: QualityOut
    model: str
    provider: str
    prompt_version: int


class TranscriptTurn(_In):
    role: Literal["agent", "caller"]
    text: str = Field(max_length=5000)


class PostprocessIn(_In):
    call_id: uuid.UUID
    contact_id: uuid.UUID | None = None
    agent_id: uuid.UUID
    transcript: list[TranscriptTurn] = Field(max_length=2000)


class PostprocessOut(BaseModel):
    summary: str
    outcome: str
    qualified: bool | None
    follow_up: str | None = None

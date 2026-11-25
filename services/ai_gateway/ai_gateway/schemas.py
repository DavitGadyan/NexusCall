"""Wire contracts of the gateway's internal API (docs/BUILD_CONVENTIONS.md §5, ai-gateway)."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Purpose = Literal["voice_turn", "classification", "summary", "email_draft", "call_postprocess", "eval"]
Classification = Literal["public", "internal", "confidential", "restricted"]
# Sources whose content is ALWAYS untrusted, whatever the caller claims (OWASP LLM01).
UNTRUSTED_SOURCES = frozenset(
    {"email", "transcript", "document", "tool", "web", "crm", "attachment", "user", "knowledge", "memory"}
)
TRUSTABLE_SOURCES = frozenset({"agent", "operator"})
Source = Literal[
    "email",
    "transcript",
    "document",
    "tool",
    "web",
    "crm",
    "attachment",
    "user",
    "knowledge",
    "memory",
    "agent",
    "operator",
]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContentIn(_M):
    text: str = Field(min_length=0, max_length=50_000)
    source: Source = "user"
    trust: Literal["trusted", "untrusted"] = "untrusted"

    @property
    def effective_trust(self) -> str:
        """Callers cannot upgrade external content to trusted."""
        if self.source in UNTRUSTED_SOURCES:
            return "untrusted"
        return self.trust


class MessageIn(_M):
    role: Literal["user", "assistant"]
    content: list[ContentIn] = Field(min_length=1, max_length=50)


class ToolContext(_M):
    call_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    thread_id: uuid.UUID | None = None


class ResponseRequest(_M):
    purpose: Purpose
    agent_id: uuid.UUID | None = None
    model: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9.-]{2,79}$")
    data_classification: Classification = "internal"
    system: str = Field(min_length=1, max_length=30_000)
    messages: list[MessageIn] = Field(min_length=1, max_length=60)
    response_schema: dict[str, Any] | None = None
    tool_ids: list[str] = Field(default_factory=list, max_length=16)
    context: ToolContext = Field(default_factory=ToolContext)
    max_tokens: int = Field(default=1024, ge=1, le=16_000)

    @field_validator("tool_ids")
    @classmethod
    def _tool_ids(cls, v: list[str]) -> list[str]:
        import re

        bad = [t for t in v if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}\.[a-z][a-z0-9_]{1,40}", t)]
        if bad:
            raise ValueError("invalid tool id")
        return sorted(set(v))

    @field_validator("messages")
    @classmethod
    def _first_is_user(cls, v: list[MessageIn]) -> list[MessageIn]:
        if v[0].role != "user":
            raise ValueError("the first message must have role 'user'")
        return v


class Usage(_M):
    input_tokens: int
    output_tokens: int
    cost_micros: int


class ToolCallOut(_M):
    id: str
    tool_id: str
    status: Literal["executed", "approval_required", "denied", "invalid_arguments", "not_granted", "failed", "skipped"]
    approval_id: str | None = None


class ResponseOut(_M):
    request_id: uuid.UUID
    text: str
    json_: Any = Field(default=None, alias="json")
    stop_reason: str
    tool_calls: list[ToolCallOut]
    usage: Usage
    model: str
    provider: str
    flags: list[str]

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class EmbeddingsRequest(_M):
    texts: list[str] = Field(min_length=1, max_length=64)
    purpose: Literal["knowledge_index", "knowledge_query"]
    data_classification: Classification = "internal"

    @field_validator("texts")
    @classmethod
    def _sizes(cls, v: list[str]) -> list[str]:
        if any(len(t) > 8_000 for t in v):
            raise ValueError("each text must be at most 8000 characters")
        return v


class EmbeddingsOut(_M):
    vectors: list[list[float]]
    model: str
    dims: int

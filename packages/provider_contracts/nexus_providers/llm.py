from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol

from pydantic import BaseModel


class ContentBlock(BaseModel):
    """`trust` labels provenance. Untrusted blocks are wrapped as data, never instructions."""

    text: str
    source: str = "user"
    trust: Literal["system", "trusted", "untrusted"] = "untrusted"


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: list[ContentBlock]


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class LlmRequest(BaseModel):
    model: str
    system: str
    messages: list[Message]
    max_tokens: int = 1024
    temperature: float = 0.2
    tools: list[ToolSpec] = []
    response_schema: dict[str, Any] | None = None
    timeout_seconds: float = 30.0


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class LlmResponse(BaseModel):
    text: str
    tool_calls: list[ToolCall] = []
    input_tokens: int
    output_tokens: int
    stop_reason: str
    model: str


class LlmStreamEvent(BaseModel):
    type: Literal["text_delta", "tool_call", "done"]
    text: str = ""
    tool_call: ToolCall | None = None
    response: LlmResponse | None = None


class LlmProvider(Protocol):
    name: str

    async def complete(self, api_key: str, req: LlmRequest) -> LlmResponse: ...

    def stream(self, api_key: str, req: LlmRequest) -> AsyncIterator[LlmStreamEvent]: ...

    async def embed(self, api_key: str, model: str, texts: list[str]) -> list[list[float]]: ...

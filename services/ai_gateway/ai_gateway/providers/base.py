from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


class ProviderError(Exception):
    """Base class. `code` is a short, non-sensitive identifier (never provider response bodies)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProviderTransientError(ProviderError):
    """Timeout, rate limit, overload, 5xx, connection failure: counts against the circuit breaker."""


class ProviderPermanentError(ProviderError):
    """Bad request, authentication, permission: not retried, not failed over."""


@dataclass(frozen=True, slots=True)
class ToolUse:
    id: str
    name: str  # provider-safe tool name (mapped back to a registered tool id by the gateway)
    input: Any


@dataclass
class ProviderRequest:
    model: str
    system: str
    messages: list[dict[str, Any]]  # Anthropic message shape; gateway-assembled, never caller-supplied raw
    max_tokens: int
    tools: list[dict[str, Any]] = field(default_factory=list)
    response_schema: dict[str, Any] | None = None
    effort: Literal["low", "medium", "high"] | None = None
    timeout_seconds: float = 60.0


@dataclass
class ProviderResponse:
    text: str
    tool_uses: list[ToolUse]
    raw_content: list[dict[str, Any]]  # assistant content to append back unchanged (append-only history)
    stop_reason: str
    input_tokens: int
    output_tokens: int
    model: str


@dataclass
class ProviderStreamEvent:
    type: Literal["text_delta", "final"]
    text: str = ""
    response: ProviderResponse | None = None


class LlmProvider(Protocol):
    name: str

    async def complete(self, api_key: str, req: ProviderRequest) -> ProviderResponse: ...

    def stream(self, api_key: str, req: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]: ...


class EmbeddingProvider(Protocol):
    name: str

    async def embed(self, api_key: str, model: str, texts: list[str], dims: int) -> list[list[float]]: ...

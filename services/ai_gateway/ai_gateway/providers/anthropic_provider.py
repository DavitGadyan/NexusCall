"""Anthropic Messages API adapter (official async SDK).

* Endpoint is fixed by configuration (`ANTHROPIC_BASE_URL` setting), never by request input.
* No `temperature`/`top_p` (rejected by current API versions) and no forced `tool_choice`
  (rejected on the 5.5 models): tools run with `auto`; the gateway validates every proposed call.
* Refusals (`stop_reason == "refusal"`) are surfaced as a normal response; the gateway's routing
  policy decides about failover (server-side fallbacks are not used, so a refused request can never
  be re-run on a model outside the tenant's allowlist).
* Errors are mapped to transient/permanent classes without copying provider response bodies.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anthropic

from ai_gateway.providers.base import (
    ProviderPermanentError,
    ProviderRequest,
    ProviderResponse,
    ProviderStreamEvent,
    ProviderTransientError,
    ToolUse,
)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, base_url: str, *, http_client: Any = None, max_retries: int = 1) -> None:
        self._base_url = base_url
        self._http_client = http_client
        self._max_retries = max_retries
        self._clients: dict[str, anthropic.AsyncAnthropic] = {}

    def _client(self, api_key: str) -> anthropic.AsyncAnthropic:
        # One client per credential (tenant connection or platform key); the key stays in memory only.
        client = self._clients.get(api_key)
        if client is None:
            client = anthropic.AsyncAnthropic(
                api_key=api_key, base_url=self._base_url, max_retries=self._max_retries, http_client=self._http_client
            )
            self._clients[api_key] = client
        return client

    @staticmethod
    def _params(req: ProviderRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "system": req.system,
            "messages": req.messages,
        }
        output_config: dict[str, Any] = {}
        if req.effort:
            output_config["effort"] = req.effort
        if req.tools:
            params["tools"] = req.tools
        elif req.response_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": req.response_schema}
        if output_config:
            params["output_config"] = output_config
        return params

    @staticmethod
    def _to_response(message: Any) -> ProviderResponse:
        text_parts: list[str] = []
        tool_uses: list[ToolUse] = []
        raw: list[dict[str, Any]] = []
        for block in message.content:
            raw.append(block.model_dump(mode="json", exclude_none=True))
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(ToolUse(id=block.id, name=block.name, input=block.input))
        usage = message.usage
        return ProviderResponse(
            text="".join(text_parts),
            tool_uses=tool_uses,
            raw_content=raw,
            stop_reason=str(message.stop_reason or "unknown"),
            input_tokens=int(usage.input_tokens or 0),
            output_tokens=int(usage.output_tokens or 0),
            model=str(message.model),
        )

    @staticmethod
    def _map_error(exc: Exception) -> Exception:
        if isinstance(exc, anthropic.APITimeoutError):
            return ProviderTransientError("provider_timeout")
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderTransientError("provider_unreachable")
        if isinstance(exc, anthropic.RateLimitError):
            return ProviderTransientError("provider_rate_limited")
        if isinstance(exc, anthropic.APIStatusError):
            if exc.status_code >= 500 or exc.status_code in (408, 409):
                return ProviderTransientError(f"provider_http_{exc.status_code}")
            return ProviderPermanentError(f"provider_http_{exc.status_code}")
        return exc

    async def complete(self, api_key: str, req: ProviderRequest) -> ProviderResponse:
        client = self._client(api_key).with_options(timeout=req.timeout_seconds)
        try:
            message = await client.messages.create(**self._params(req))
        except anthropic.APIError as exc:
            raise self._map_error(exc) from None
        return self._to_response(message)

    async def stream(self, api_key: str, req: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        client = self._client(api_key).with_options(timeout=req.timeout_seconds)
        try:
            async with client.messages.stream(**self._params(req)) as stream:
                async for event in stream:
                    if event.type == "text":
                        yield ProviderStreamEvent(type="text_delta", text=event.text)
                final = await stream.get_final_message()
        except anthropic.APIError as exc:
            raise self._map_error(exc) from None
        yield ProviderStreamEvent(type="final", response=self._to_response(final))

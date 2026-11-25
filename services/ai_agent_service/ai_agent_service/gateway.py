"""Client for the AI Gateway internal API (docs/BUILD_CONVENTIONS.md §5, ai-gateway). This service never
talks to a model provider directly: every completion and embedding goes through the gateway's policy pipeline.

Request bodies follow the gateway contract exactly. There are no sampling parameters (the gateway rejects
them, and the Messages API version it sends no longer accepts them). External content is always sent as
untrusted blocks with its real source label."""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from redis.asyncio import Redis

from nexus_core.clients import ServiceClient, UpstreamError, UpstreamUnavailable, tenant_settings_section
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.errors import ProblemError, ServiceUnavailable
from nexus_core.storage import ObjectStore

UNTRUSTED_SOURCES = frozenset({"email", "transcript", "document", "knowledge", "tool", "crm", "attachment", "user"})


class Deps(Protocol):
    """What background jobs and request handlers need (TaskRuntime in workers, AppDeps in the API)."""

    @property
    def db(self) -> Database: ...

    @property
    def store(self) -> ObjectStore: ...

    @property
    def redis(self) -> Redis | None: ...

    @property
    def settings(self) -> Any: ...

    def client(self, service: str, *, timeout: float = 10.0) -> ServiceClient: ...


class AppDeps:
    def __init__(self, app: Any) -> None:
        self._app = app

    @property
    def db(self) -> Database:
        return self._app.state.db  # type: ignore[no-any-return]

    @property
    def store(self) -> ObjectStore:
        value = getattr(self._app.state, "object_store", None)
        if value is None:
            raise ServiceUnavailable("Object storage is not configured")
        return value  # type: ignore[no-any-return]

    @property
    def redis(self) -> Redis | None:
        return getattr(self._app.state, "redis", None)

    @property
    def settings(self) -> Any:
        return self._app.state.settings

    def client(self, service: str, *, timeout: float = 10.0) -> ServiceClient:
        from nexus_core.clients import service_client

        return service_client(self._app, service, timeout=timeout)


# ------------------------------------------------------------------------------------------- errors
class GatewayDenied(ProblemError):
    """The gateway's policy pipeline denied the request (classification, budget, kill switch, AI disabled...)."""

    status, code, title = 403, "policy_denied", "Denied by AI policy"

    def __init__(self, reason: str) -> None:
        super().__init__("The AI gateway denied this request", extra={"reason": reason})
        self.reason = reason


class GatewayInvalidOutput(ProblemError):
    status, code, title = 502, "invalid_model_output", "The model output did not match the required schema"


class GatewayRateLimited(ProblemError):
    status, code, title = 429, "rate_limited", "AI request rate exceeded"


class GatewayUnavailable(ServiceUnavailable):
    pass


# ------------------------------------------------------------------------------------------- request helpers
def block(text: str, source: str, *, trusted: bool = False) -> dict[str, str]:
    """A content block. External sources are always untrusted, whatever the caller asked for."""
    trust = "trusted" if trusted and source not in UNTRUSTED_SOURCES else "untrusted"
    return {"text": text, "source": source, "trust": trust}


@dataclass(slots=True)
class GatewayResult:
    request_id: uuid.UUID
    text: str
    json: Any
    stop_reason: str
    tool_calls: list[dict[str, Any]]
    model: str
    provider: str
    flags: list[str]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class Gateway:
    def __init__(self, deps: Deps) -> None:
        self._deps = deps
        self._timeout = float(getattr(deps.settings, "gateway_timeout_seconds", 130.0))

    @property
    def _client(self) -> ServiceClient:
        return self._deps.client("ai-gateway", timeout=self._timeout)

    async def _post(self, path: str, body: dict[str, Any], tenant_id: uuid.UUID, actor: Actor) -> dict[str, Any]:
        try:
            resp = await self._client.request(
                "POST",
                path,
                json_body=body,
                tenant_id=tenant_id,
                actor=actor,
                timeout_seconds=self._timeout,
                expect=(200, 403, 429, 502),
            )
        except UpstreamUnavailable as exc:
            raise GatewayUnavailable("The AI gateway is unavailable") from exc
        except UpstreamError as exc:
            raise GatewayUnavailable(f"The AI gateway responded with {exc.upstream_status}") from exc
        data: dict[str, Any] = {}
        with contextlib.suppress(ValueError):
            data = resp.json()
        if resp.status_code == 200:
            return data
        reason = str(data.get("reason") or data.get("code") or "unknown")
        if resp.status_code == 403:
            raise GatewayDenied(reason)
        if resp.status_code == 429:
            raise GatewayRateLimited("AI request rate exceeded; try again later")
        if reason == "invalid_model_output":
            raise GatewayInvalidOutput()
        raise GatewayUnavailable("The AI gateway could not complete the request")

    async def respond(
        self,
        *,
        tenant_id: uuid.UUID,
        actor: Actor,
        purpose: str,
        system: str,
        blocks: list[dict[str, str]],
        agent_id: uuid.UUID | None = None,
        model: str | None = None,
        data_classification: str = "internal",
        response_schema: dict[str, Any] | None = None,
        tool_ids: list[str] | None = None,
        context: dict[str, str] | None = None,
        max_tokens: int = 1024,
    ) -> GatewayResult:
        body: dict[str, Any] = {
            "purpose": purpose,
            "data_classification": data_classification,
            "system": system,
            "messages": [{"role": "user", "content": blocks}],
            "tool_ids": list(tool_ids or []),
            "max_tokens": max_tokens,
        }
        if agent_id is not None:
            body["agent_id"] = str(agent_id)
        if model:
            body["model"] = model
        if response_schema is not None:
            body["response_schema"] = response_schema
        if context:
            body["context"] = context
        data = await self._post("/internal/v1/ai/responses", body, tenant_id, actor)
        usage = data.get("usage") or {}
        try:
            return GatewayResult(
                request_id=uuid.UUID(str(data["request_id"])),
                text=str(data.get("text") or ""),
                json=data.get("json"),
                stop_reason=str(data.get("stop_reason") or ""),
                tool_calls=[t for t in data.get("tool_calls") or [] if isinstance(t, dict)],
                model=str(data.get("model") or ""),
                provider=str(data.get("provider") or ""),
                flags=[str(f) for f in data.get("flags") or []],
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                cost_micros=int(usage.get("cost_micros", 0)),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise GatewayUnavailable("The AI gateway returned an unexpected response") from exc

    async def embed(
        self, *, tenant_id: uuid.UUID, actor: Actor, texts: list[str], purpose: str, data_classification: str
    ) -> tuple[list[list[float]], str]:
        data = await self._post(
            "/internal/v1/ai/embeddings",
            {"texts": texts, "purpose": purpose, "data_classification": data_classification},
            tenant_id,
            actor,
        )
        vectors = data.get("vectors")
        model = str(data.get("model") or "")
        if not isinstance(vectors, list) or len(vectors) != len(texts) or not model:
            raise GatewayUnavailable("The AI gateway returned an unexpected embedding response")
        return [[float(x) for x in v] for v in vectors], model


async def tenant_ai_policy(deps: Deps, tenant_id: uuid.UUID) -> dict[str, Any]:
    """Tenant `ai` settings from tenant-service (5 s cache). Fails closed when unavailable."""
    try:
        return await tenant_settings_section(deps.client("tenant-service"), deps.redis, tenant_id, "ai")
    except (UpstreamError, UpstreamUnavailable) as exc:
        raise ServiceUnavailable("Tenant AI policy is unavailable; failing closed") from exc

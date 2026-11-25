"""Authenticated service-to-service HTTP client (ADR-0006).

Adds the caller's service token, the verified tenant/actor context and correlation ids.
Every call has a timeout. Upstream problem responses are surfaced as `UpstreamError`
without leaking response bodies into logs.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from redis.asyncio import Redis

from nexus_core.context import Actor, correlation_id_var, request_id_var
from nexus_core.errors import ProblemError


class UpstreamError(ProblemError):
    status, code, title = 502, "upstream_error", "Upstream service error"

    def __init__(self, service: str, upstream_status: int, upstream_code: str | None, detail: str | None = None):
        super().__init__(detail or f"{service} responded with {upstream_status}")
        self.service = service
        self.upstream_status = upstream_status
        self.upstream_code = upstream_code

    @property
    def is_not_found(self) -> bool:
        return self.upstream_status == 404

    @property
    def is_denied(self) -> bool:
        return self.upstream_status in (401, 403)


class UpstreamUnavailable(ProblemError):
    status, code, title = 503, "upstream_unavailable", "Upstream service unavailable"


class ServiceClient:
    def __init__(
        self,
        service: str,
        base_url: str,
        token_source: Callable[[], Awaitable[str]],
        *,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.service = service
        self._base = base_url.rstrip("/")
        self._token = token_source
        self._timeout = timeout
        self._transport = transport

    async def request(
        self,
        method: str,
        path: str,
        *,
        tenant_id: uuid.UUID | str | None = None,
        actor: Actor | None = None,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        timeout_seconds: float | None = None,
        expect: tuple[int, ...] = (200, 201, 202, 204),
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        if tenant_id is not None:
            headers["X-Tenant-ID"] = str(tenant_id)
        if actor is not None:
            headers["X-Actor"] = str(actor)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if (cid := correlation_id_var.get()) is not None:
            headers["X-Correlation-ID"] = cid
        if (rid := request_id_var.get()) is not None:
            headers["X-Request-ID"] = rid
        try:
            async with httpx.AsyncClient(
                timeout=timeout_seconds or self._timeout, transport=self._transport, follow_redirects=False
            ) as client:
                resp = await client.request(
                    method, f"{self._base}{path}", json=json_body, params=params, headers=headers
                )
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"{self.service} is unavailable") from exc
        if resp.status_code not in expect:
            code = None
            with contextlib.suppress(ValueError, AttributeError):
                code = resp.json().get("code")
            raise UpstreamError(self.service, resp.status_code, code)
        return resp

    async def get_json(self, path: str, **kw: Any) -> Any:
        return (await self.request("GET", path, **kw)).json()

    async def post_json(self, path: str, body: Any = None, **kw: Any) -> Any:
        resp = await self.request("POST", path, json_body=body, **kw)
        return resp.json() if resp.content else None


def service_client(app: Any, service: str, *, timeout: float = 5.0) -> ServiceClient:
    """Cached client for another NexusCall service, e.g. `service_client(request.app, "crm-service")`."""
    cache: dict[str, ServiceClient] | None = getattr(app.state, "service_clients", None)
    if cache is None:
        cache = {}
        app.state.service_clients = cache
    if service not in cache:

        async def token() -> str:
            return str(await app.state.service_token())

        cache[service] = ServiceClient(service, app.state.settings.service_url(service), token, timeout=timeout)
    return cache[service]


async def tenant_settings_section(
    client: ServiceClient, redis: Redis | None, tenant_id: uuid.UUID | str, section: str, *, ttl_seconds: int = 5
) -> dict[str, Any]:
    """Effective tenant settings from tenant-service (`communication` includes kill-switch state).

    Cached for a few seconds only, so kill switches and policy changes take effect almost immediately."""
    key = f"tenant_settings:{tenant_id}:{section}"
    if redis is not None and ttl_seconds > 0:
        cached = await redis.get(key)
        if cached:
            return dict(json.loads(cached))
    data = dict(await client.get_json(f"/internal/v1/tenants/{tenant_id}/settings/{section}"))
    if redis is not None and ttl_seconds > 0:
        await redis.set(key, json.dumps(data), ex=ttl_seconds)
    return data

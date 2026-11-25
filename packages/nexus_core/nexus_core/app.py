"""FastAPI application factory shared by every service."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI
from redis.asyncio import Redis
from sqlalchemy import Table

from nexus_core.auth.jwt import JWKSCache, TokenVerifier
from nexus_core.authz import AuthzResolver, HttpAuthzResolver
from nexus_core.config import ServiceSettings
from nexus_core.db import Database
from nexus_core.errors import install_error_handlers
from nexus_core.health import ReadinessCheck, health_router
from nexus_core.logging import configure_logging
from nexus_core.messaging import EventConsumer, Handler, OutboxRelay
from nexus_core.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from nexus_core.telemetry import install_metrics, install_tracing

Hook = Callable[[FastAPI], Awaitable[None]]
log = logging.getLogger(__name__)


async def _supervise(name: str, factory: Callable[[], Awaitable[None]]) -> None:
    """Keep a background loop alive; restart with backoff on failure (broker restarts etc.)."""
    delay = 1.0
    while True:
        try:
            await factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background_loop_failed", extra={"loop": name})
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


async def _no_token() -> str:
    raise RuntimeError("Service token provider not configured")


def create_service_app(
    settings: ServiceSettings,
    *,
    title: str,
    version: str = "0.1.0",
    routers: Sequence[APIRouter] = (),
    on_startup: Sequence[Hook] = (),
    on_shutdown: Sequence[Hook] = (),
    extra_checks: dict[str, ReadinessCheck] | None = None,
    authz_resolver: AuthzResolver | None = None,
    use_database: bool = True,
    use_redis: bool = True,
    token_source: Callable[[], Awaitable[str]] | None = None,
    outbox: Table | None = None,
    inbox: Table | None = None,
    event_handlers: dict[str, Handler] | None = None,
    allowed_producers: dict[str, set[str]] | None = None,
    consumer_name: str | None = None,
) -> FastAPI:
    configure_logging(settings.service_name, settings.env.value, settings.log_level)
    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        for hook in on_startup:
            await hook(app)
        tasks: list[asyncio.Task[None]] = []
        db_ = state.get("db")
        amqp = settings.amqp_url.get_secret_value()
        if db_ is not None and outbox is not None and settings.outbox_relay_enabled:
            relay = OutboxRelay(db_, outbox, amqp)
            tasks.append(asyncio.create_task(_supervise("outbox_relay", relay.run)))
        if db_ is not None and inbox is not None and event_handlers and settings.event_consumers_enabled:
            consumer = EventConsumer(
                consumer_name=consumer_name or settings.service_name.replace("-", "_"),
                db=db_,
                inbox=inbox,
                amqp_url=amqp,
                handlers=event_handlers,
                allowed_producers=allowed_producers,
            )
            tasks.append(asyncio.create_task(_supervise("event_consumer", consumer.run)))
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for hook in on_shutdown:
            await hook(app)
        if state.get("db"):
            await state["db"].dispose()
        if state.get("redis"):
            await state["redis"].aclose()

    docs = None if settings.is_production_like else "/docs"
    app = FastAPI(
        title=title, version=version, lifespan=lifespan, docs_url=docs, redoc_url=None, openapi_url="/openapi.json"
    )
    app.state.settings = settings

    checks: dict[str, ReadinessCheck] = dict(extra_checks or {})
    if use_database:
        db = Database(
            settings.database_url.get_secret_value(),
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
        )
        state["db"] = db
        app.state.db = db
        checks["database"] = db.ping
    redis: Redis | None = None
    if use_redis:
        redis = Redis.from_url(settings.redis_url.get_secret_value())
        state["redis"] = redis
        app.state.redis = redis

        async def _redis_ok() -> bool:
            return bool(await redis.ping())  # type: ignore[union-attr]

        checks["redis"] = _redis_ok

    if token_source is None and settings.service_client_id and settings.client_secret_value():
        from nexus_core.auth.service_token import ServiceTokenProvider

        provider = ServiceTokenProvider(
            settings.token_url, settings.service_client_id, settings.client_secret_value() or ""
        )
        token_source = provider.token
    app.state.service_token = token_source or _no_token

    app.state.token_verifier = TokenVerifier(
        JWKSCache(settings.jwks_url),
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        algorithms=settings.oidc_allowed_algs,
        leeway=settings.oidc_leeway_seconds,
    )
    app.state.authz_resolver = authz_resolver or HttpAuthzResolver(
        settings.tenant_service_url, token_source or _no_token, redis, settings.authz_cache_ttl_seconds
    )

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    install_metrics(app, settings.service_name)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    install_tracing(app, settings.service_name, settings.otel_exporter_otlp_endpoint)

    app.include_router(health_router(settings.service_name, version, checks))
    from nexus_core.rls_api import rls_router

    app.include_router(rls_router(settings))
    for router in routers:
        app.include_router(router)
    return app

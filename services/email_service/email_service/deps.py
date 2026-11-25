"""Runtime dependencies shared by the API process (periodic jobs, webhooks) and the Celery worker.

`Deps` is what background code needs. `AppDeps` adapts the FastAPI app; `WorkerDeps` adapts `TaskRuntime`.
Tests build their own object with the same attributes.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from email_service.providers.dns import DnsVerifier, build_dns_verifier
from email_service.providers.ses import OutboundProvider, build_outbound_provider
from email_service.settings import Settings
from email_service.tables import outbox
from nexus_core.audit import emit_audit
from nexus_core.celery_app import JobContext
from nexus_core.clients import ServiceClient
from nexus_core.context import Actor, current_correlation_id
from nexus_core.credentials import CredentialResolver
from nexus_core.db import Database
from nexus_core.messaging import enqueue
from nexus_events import ActorRef, make_event

PRODUCER = "email-service"
SEND_TASK = "emails.send_message"
Enqueue = Callable[..., Awaitable[None]]


class Deps(Protocol):
    @property
    def db(self) -> Database: ...

    @property
    def redis(self) -> Redis | None: ...

    @property
    def settings(self) -> Settings: ...

    @property
    def provider(self) -> OutboundProvider: ...

    @property
    def credentials(self) -> CredentialResolver: ...

    def client(self, service: str) -> ServiceClient: ...

    async def enqueue(
        self, name: str, queue: str, ctx: JobContext, *, countdown: float | None = None, **ids: str
    ) -> None: ...  # fmt: skip


def build_vault(settings: Settings) -> Any:
    if not (settings.vault_role_id_file or settings.vault_token_file):
        return None
    from nexus_core.vault import VaultClient

    return VaultClient(
        settings.vault_addr,
        kv_mount=settings.vault_kv_mount,
        role_id_file=settings.vault_role_id_file,
        secret_id_file=settings.vault_secret_id_file,
        token_file=settings.vault_token_file,
    )


async def celery_enqueue(name: str, queue: str, ctx: JobContext, *, countdown: float | None = None, **ids: str) -> None:
    from email_service.worker import celery_app
    from nexus_core.celery_app import enqueue_task

    await enqueue_task(celery_app, name, queue, ctx, countdown=countdown, **ids)


class AppDeps:
    def __init__(self, app: Any) -> None:
        from nexus_core.clients import service_client

        self._app = app
        self._client = lambda name: service_client(app, name)

    @property
    def db(self) -> Database:
        return self._app.state.db  # type: ignore[no-any-return]

    @property
    def redis(self) -> Redis | None:
        return getattr(self._app.state, "redis", None)

    @property
    def settings(self) -> Settings:
        return self._app.state.settings  # type: ignore[no-any-return]

    @property
    def provider(self) -> OutboundProvider:
        return self._app.state.provider  # type: ignore[no-any-return]

    @property
    def credentials(self) -> CredentialResolver:
        return self._app.state.credentials  # type: ignore[no-any-return]

    @property
    def dns(self) -> DnsVerifier:
        return self._app.state.dns  # type: ignore[no-any-return]

    def client(self, service: str) -> ServiceClient:
        return self._client(service)

    async def enqueue(self, name: str, queue: str, ctx: JobContext, *, countdown: float | None = None,
                      **ids: str) -> None:  # fmt: skip
        await self._app.state.enqueue(name, queue, ctx, countdown=countdown, **ids)


class WorkerDeps:
    """Built once per worker process from `TaskRuntime`."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._provider: OutboundProvider | None = None
        self._credentials: CredentialResolver | None = None

    @property
    def db(self) -> Database:
        return self._runtime.db  # type: ignore[no-any-return]

    @property
    def redis(self) -> Redis | None:
        return self._runtime.redis  # type: ignore[no-any-return]

    @property
    def settings(self) -> Settings:
        return self._runtime.settings  # type: ignore[no-any-return]

    @property
    def provider(self) -> OutboundProvider:
        if self._provider is None:
            s = self.settings
            self._provider = build_outbound_provider(
                s, configuration_set=s.ses_configuration_set, timeout=s.ses_timeout_seconds
            )
        return self._provider

    @property
    def credentials(self) -> CredentialResolver:
        if self._credentials is None:
            s = self.settings
            vault = self._runtime.vault if (s.vault_role_id_file or s.vault_token_file) else None
            self._credentials = CredentialResolver(s, vault, self._runtime.client("integration-service"))
        return self._credentials

    def client(self, service: str) -> ServiceClient:
        return self._runtime.client(service)  # type: ignore[no-any-return]

    async def enqueue(self, name: str, queue: str, ctx: JobContext, *, countdown: float | None = None,
                      **ids: str) -> None:  # fmt: skip
        await celery_enqueue(name, queue, ctx, countdown=countdown, **ids)


def app_deps(request: Request) -> AppDeps:
    return AppDeps(request.app)


def make_dns(settings: Settings) -> DnsVerifier:
    return build_dns_verifier(settings, timeout_seconds=settings.dns_timeout_seconds)


# ------------------------------------------------------------------------------------------- events / audit
async def emit(
    session: AsyncSession,
    event_type: str,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    payload: dict[str, Any],
    causation_id: uuid.UUID | None = None,
) -> None:
    event = make_event(
        event_type,
        producer=PRODUCER,
        actor=ActorRef(type=actor.type, id=actor.id),  # type: ignore[arg-type]
        correlation_id=current_correlation_id(),
        tenant_id=tenant_id,
        payload=payload,
        causation_id=causation_id,
    )
    await enqueue(session, outbox, event)


async def audit(
    session: AsyncSession,
    actor: Actor,
    action: str,
    resource_type: str,
    resource_id: Any = None,
    *,
    tenant_id: uuid.UUID,
    outcome: str = "success",
    **metadata: Any,
) -> None:
    await emit_audit(
        session,
        outbox,
        producer=PRODUCER,
        actor=actor,
        tenant_id=tenant_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        metadata=metadata,
    )


def job_context(tenant_id: uuid.UUID, actor: Actor) -> JobContext:
    return JobContext(
        tenant_id=tenant_id,
        correlation_id=uuid.UUID(current_correlation_id()) if _is_uuid(current_correlation_id()) else uuid.uuid4(),
        actor_type=actor.type,
        actor_id=actor.id,
    )


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def queue_for(settings: Settings, category: str) -> str:
    return "emails.campaign_send" if category == "campaign" else settings.transactional_queue

"""Runtime dependencies shared by request handlers, Celery tasks and periodic jobs.

Task bodies are plain coroutines taking a `Deps`, so tests call them directly (BUILD_CONVENTIONS §3 async work).
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from redis.asyncio import Redis

from call_service.providers import select_telephony
from call_service.providers.twilio import TwilioRest
from call_service.settings import Settings
from nexus_core.celery_app import JobContext
from nexus_core.clients import ServiceClient, service_client
from nexus_core.config import ProviderMode
from nexus_core.credentials import CredentialResolver, ResolvedCredentials
from nexus_core.db import Database
from nexus_providers.telephony import TelephonyCredentials

PRODUCER = "call-service"
# Simulator account (development/test only): the simulator accepts it with FAKE_PROVIDER_SECRET as auth token.
FAKE_ACCOUNT_SID = "AC" + "0" * 31 + "1"


class Deps(Protocol):
    settings: Settings
    db: Database
    redis: Redis | None
    telephony: TwilioRest
    credentials: CredentialResolver

    def client(self, service: str) -> ServiceClient: ...

    async def enqueue(self, name: str, queue: str, ctx: JobContext, **ids: str) -> None: ...


def telephony_credentials(resolved: ResolvedCredentials) -> TelephonyCredentials:
    if resolved.source == "fake":
        return TelephonyCredentials(account_sid=FAKE_ACCOUNT_SID, auth_token=resolved.secret("fake_secret"))
    return TelephonyCredentials(account_sid=resolved.secret("account_sid"), auth_token=resolved.secret("auth_token"))


async def twilio_credentials(deps: Deps, tenant_id: uuid.UUID) -> TelephonyCredentials:
    return telephony_credentials(await deps.credentials.resolve(tenant_id, "twilio"))


async def celery_enqueue(name: str, queue: str, ctx: JobContext, **ids: str) -> None:
    from call_service.worker import celery_app
    from nexus_core.celery_app import enqueue_task

    await enqueue_task(celery_app, name, queue, ctx, **ids)


class AppDeps:
    """Deps view over a FastAPI app's state."""

    def __init__(self, app: Any) -> None:
        self.app = app

    @property
    def settings(self) -> Settings:
        return self.app.state.settings  # type: ignore[no-any-return]

    @property
    def db(self) -> Database:
        return self.app.state.db  # type: ignore[no-any-return]

    @property
    def redis(self) -> Redis | None:
        return getattr(self.app.state, "redis", None)

    @property
    def telephony(self) -> TwilioRest:
        return self.app.state.telephony  # type: ignore[no-any-return]

    @property
    def credentials(self) -> CredentialResolver:
        return self.app.state.credentials  # type: ignore[no-any-return]

    def client(self, service: str) -> ServiceClient:
        return service_client(self.app, service)

    async def enqueue(self, name: str, queue: str, ctx: JobContext, **ids: str) -> None:
        await self.app.state.enqueue(name, queue, ctx, **ids)


class WorkerDeps:
    """Deps for Celery tasks, built on `nexus_core.celery_app.TaskRuntime`."""

    def __init__(self, runtime: Any, settings: Settings) -> None:
        self.runtime = runtime
        self.settings = settings
        self._telephony: TwilioRest | None = None
        self._credentials: CredentialResolver | None = None

    @property
    def db(self) -> Database:
        return self.runtime.db  # type: ignore[no-any-return]

    @property
    def redis(self) -> Redis | None:
        return self.runtime.redis  # type: ignore[no-any-return]

    @property
    def telephony(self) -> TwilioRest:
        if self._telephony is None:
            self._telephony = select_telephony(self.settings)
        return self._telephony

    @property
    def credentials(self) -> CredentialResolver:
        if self._credentials is None:
            fake = self.settings.provider_mode is ProviderMode.FAKE
            self._credentials = CredentialResolver(
                self.settings,
                None if fake else self.runtime.vault,
                None if fake else self.runtime.client("integration-service"),
            )
        return self._credentials

    def client(self, service: str) -> ServiceClient:
        return self.runtime.client(service)  # type: ignore[no-any-return]

    async def enqueue(self, name: str, queue: str, ctx: JobContext, **ids: str) -> None:
        await celery_enqueue(name, queue, ctx, **ids)

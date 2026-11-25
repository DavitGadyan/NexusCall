from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI

from call_service.api import routers
from call_service.consumers import ALLOWED_PRODUCERS, HANDLERS
from call_service.deps import AppDeps, celery_enqueue
from call_service.providers import select_telephony
from call_service.settings import Settings, get_settings
from call_service.sweeper import reconcile_due, sweep
from call_service.tables import inbox, outbox
from nexus_core.app import create_service_app
from nexus_core.authz import AuthzResolver
from nexus_core.clients import service_client
from nexus_core.config import ProviderMode
from nexus_core.credentials import CredentialResolver
from nexus_core.scheduling import PeriodicJob
from nexus_core.vault import VaultClient


def create_app(
    settings: Settings | None = None,
    *,
    authz_resolver: AuthzResolver | None = None,
    enqueue: Any = None,
    telephony_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or get_settings()

    async def startup(app: FastAPI) -> None:
        app.state.enqueue = enqueue or celery_enqueue
        app.state.telephony = select_telephony(settings, transport=telephony_transport)
        vault = None
        if settings.provider_mode is ProviderMode.REAL and (settings.vault_role_id_file or settings.vault_token_file):
            vault = VaultClient(
                settings.vault_addr,
                kv_mount=settings.vault_kv_mount,
                role_id_file=settings.vault_role_id_file,
                secret_id_file=settings.vault_secret_id_file,
                token_file=settings.vault_token_file,
            )
        app.state.vault = vault
        app.state.credentials = CredentialResolver(settings, vault, service_client(app, "integration-service"))

    async def shutdown(app: FastAPI) -> None:
        if getattr(app.state, "vault", None) is not None:
            await app.state.vault.close()

    async def sweeper(app: FastAPI) -> None:
        await sweep(AppDeps(app))

    async def reconciliation(app: FastAPI) -> None:
        await reconcile_due(AppDeps(app))

    return create_service_app(
        settings,
        title="NexusCall Call Orchestration Service",
        routers=routers,
        outbox=outbox,
        inbox=inbox,
        event_handlers=HANDLERS,
        allowed_producers=ALLOWED_PRODUCERS,
        authz_resolver=authz_resolver,
        on_startup=[startup],
        on_shutdown=[shutdown],
        periodic_jobs=[
            PeriodicJob("call-sweeper", settings.sweeper_interval_seconds, sweeper),
            PeriodicJob("call-reconciliation", settings.reconcile_interval_seconds, reconciliation),
        ],
    )


app = create_app()

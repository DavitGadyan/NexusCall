from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from email_service.api import routers
from email_service.api.webhooks import build_verifier
from email_service.deps import AppDeps, build_vault, celery_enqueue, make_dns
from email_service.jobs import scheduler_tick, suppression_sync_tick, sweeper_tick
from email_service.providers.ses import build_outbound_provider
from email_service.settings import Settings, get_settings
from email_service.tables import inbox, outbox
from nexus_core.app import create_service_app
from nexus_core.authz import AuthzResolver
from nexus_core.credentials import CredentialResolver
from nexus_core.scheduling import PeriodicJob


def create_app(
    settings: Settings | None = None,
    *,
    authz_resolver: AuthzResolver | None = None,
    enqueue: Any = None,
    provider: Any = None,
    dns: Any = None,
    credentials: Any = None,
) -> FastAPI:
    settings = settings or get_settings()

    async def startup(app: FastAPI) -> None:
        app.state.enqueue = enqueue or celery_enqueue
        app.state.provider = provider or build_outbound_provider(
            settings, configuration_set=settings.ses_configuration_set, timeout=settings.ses_timeout_seconds
        )
        app.state.dns = dns or make_dns(settings)
        app.state.vault = build_vault(settings)
        if credentials is not None:
            app.state.credentials = credentials
        else:
            from nexus_core.clients import service_client

            app.state.credentials = CredentialResolver(
                settings, app.state.vault, service_client(app, "integration-service")
            )
        app.state.sns_verifier = build_verifier(settings)

    async def shutdown(app: FastAPI) -> None:
        if getattr(app.state, "vault", None) is not None:
            await app.state.vault.close()

    async def schedule(app: FastAPI) -> None:
        await scheduler_tick(AppDeps(app))

    async def sweep(app: FastAPI) -> None:
        await sweeper_tick(AppDeps(app))

    async def sync(app: FastAPI) -> None:
        await suppression_sync_tick(AppDeps(app))

    return create_service_app(
        settings,
        title="NexusCall Email Campaign Service",
        routers=routers,
        outbox=outbox,
        inbox=inbox,
        authz_resolver=authz_resolver,
        on_startup=[startup],
        on_shutdown=[shutdown],
        periodic_jobs=[
            PeriodicJob("email-campaign-scheduler", settings.scheduler_interval_seconds, schedule),
            PeriodicJob("email-sweeper", settings.sweeper_interval_seconds, sweep),
            PeriodicJob("email-suppression-sync", settings.suppression_sync_interval_seconds, sync),
        ],
    )


app = create_app()

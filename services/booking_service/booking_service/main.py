from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from booking_service.api import routers
from booking_service.providers.calendar import GoogleTokenSource, build_calendar, token_url_for
from booking_service.service import expire_holds, send_reminders, sync_calendars
from booking_service.settings import Settings, get_settings
from booking_service.tables import inbox, outbox
from nexus_core.app import create_service_app
from nexus_core.authz import AuthzResolver
from nexus_core.clients import service_client
from nexus_core.credentials import CredentialResolver
from nexus_core.scheduling import PeriodicJob
from nexus_core.vault import VaultClient


def create_app(
    settings: Settings | None = None,
    *,
    authz_resolver: AuthzResolver | None = None,
    calendar: Any = None,
    calendar_tokens: Any = None,
    credentials: CredentialResolver | None = None,
) -> FastAPI:
    """`calendar`, `calendar_tokens` and `credentials` are test seams; production wiring is built here."""
    settings = settings or get_settings()

    async def startup(app: FastAPI) -> None:
        vault = None
        if settings.vault_role_id_file or settings.vault_token_file:
            vault = VaultClient(
                settings.vault_addr,
                kv_mount=settings.vault_kv_mount,
                role_id_file=settings.vault_role_id_file,
                secret_id_file=settings.vault_secret_id_file,
                token_file=settings.vault_token_file,
            )
        app.state.vault = vault
        resolver = credentials or CredentialResolver(settings, vault, service_client(app, "integration-service"))
        app.state.credentials = resolver
        app.state.calendar = calendar or build_calendar(settings)
        app.state.calendar_tokens = calendar_tokens or GoogleTokenSource(
            resolver, token_url_for(settings), timeout=settings.calendar_timeout_seconds
        )

    async def shutdown(app: FastAPI) -> None:
        if getattr(app.state, "vault", None) is not None:
            await app.state.vault.close()

    async def _expire(app: FastAPI) -> None:
        await expire_holds(app)

    async def _remind(app: FastAPI) -> None:
        await send_reminders(app)

    async def _sync(app: FastAPI) -> None:
        await sync_calendars(app)

    return create_service_app(
        settings,
        title="NexusCall Booking Service",
        routers=routers,
        outbox=outbox,
        inbox=inbox,
        authz_resolver=authz_resolver,
        on_startup=[startup],
        on_shutdown=[shutdown],
        periodic_jobs=[
            PeriodicJob("tentative-hold-expiry", 30, _expire),
            PeriodicJob("booking-reminders", 300, _remind),
            PeriodicJob("calendar-sync", 15, _sync),
        ],
    )


app = create_app()

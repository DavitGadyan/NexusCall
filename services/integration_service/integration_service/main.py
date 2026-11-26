from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI

from integration_service.api import routers
from integration_service.connections import retry_failed_deletions
from integration_service.jobs import rotation_reminders
from integration_service.oauth import purge_states
from integration_service.providers.testers import ProviderTester
from integration_service.secrets import SecretStore, hmac_key
from integration_service.settings import Settings, get_settings
from integration_service.tables import inbox, outbox
from integration_service.webhooks import EVENT_HANDLERS, SUBSCRIBABLE, Publisher, sweep
from nexus_core.app import create_service_app
from nexus_core.authz import AuthzResolver
from nexus_core.scheduling import PeriodicJob
from nexus_core.vault import VaultClient


def create_app(
    settings: Settings | None = None,
    *,
    authz_resolver: AuthzResolver | None = None,
    vault: VaultClient | None = None,
    testers: Any = None,
    egress_transport: httpx.AsyncBaseTransport | None = None,
    publisher: Publisher | None = None,
) -> FastAPI:
    """`vault`, `testers`, `egress_transport` and `publisher` are test seams; production wiring is built here."""
    settings = settings or get_settings()

    async def startup(app: FastAPI) -> None:
        client = vault
        if client is None and (settings.vault_role_id_file or settings.vault_token_file):
            client = VaultClient(
                settings.vault_addr,
                kv_mount=settings.vault_kv_mount,
                role_id_file=settings.vault_role_id_file,
                secret_id_file=settings.vault_secret_id_file,
                token_file=settings.vault_token_file,
            )
        app.state.vault = client
        app.state.secrets = SecretStore(client)
        app.state.hmac_key = hmac_key(settings)
        app.state.egress_transport = egress_transport
        app.state.testers = testers or ProviderTester(settings, transport=egress_transport)
        if publisher is not None:
            app.state.publish = publisher
        else:
            from integration_service.worker import publish

            app.state.publish = publish

    async def shutdown(app: FastAPI) -> None:
        if vault is None and getattr(app.state, "vault", None) is not None:
            await app.state.vault.close()

    async def _sweep(app: FastAPI) -> None:
        await sweep(app.state.db, app.state.publish, batch=settings.webhook_sweep_batch)

    async def _reminders(app: FastAPI) -> None:
        await rotation_reminders(app)

    async def _maintenance(app: FastAPI) -> None:
        await purge_states(app)
        await retry_failed_deletions(app)

    return create_service_app(
        settings,
        title="NexusCall Integration & Webhook Service",
        routers=routers,
        outbox=outbox,
        inbox=inbox,
        authz_resolver=authz_resolver,
        event_handlers=dict(EVENT_HANDLERS),
        allowed_producers={k: set(v) for k, v in SUBSCRIBABLE.items()},
        on_startup=[startup],
        on_shutdown=[shutdown],
        periodic_jobs=[
            PeriodicJob("webhook-sweeper", 5, _sweep),
            PeriodicJob("rotation-reminders", 3600, _reminders),
            PeriodicJob("integration-maintenance", 600, _maintenance),
        ],
    )


app = create_app()

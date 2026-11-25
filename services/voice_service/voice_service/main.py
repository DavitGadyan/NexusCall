from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI

from nexus_core.app import create_service_app
from nexus_core.authz import AuthzResolver
from nexus_core.clients import service_client
from nexus_core.config import ProviderMode
from nexus_core.credentials import CredentialResolver
from nexus_core.scheduling import PeriodicJob
from nexus_core.storage import ObjectStore
from nexus_core.vault import VaultClient
from voice_service.api import routers
from voice_service.deps import AppDeps, celery_enqueue
from voice_service.jobs import postprocess_sweep, retention_purge
from voice_service.llm import GatewayStream
from voice_service.providers import select_tts
from voice_service.settings import Settings, get_settings
from voice_service.tables import inbox, outbox

log = logging.getLogger(__name__)


class _UnconfiguredStore:
    """Development/test placeholder when S3 credentials are absent: every operation fails loudly."""

    def __getattr__(self, name: str) -> Any:
        async def _fail(*_: Any, **__: Any) -> Any:
            raise RuntimeError("Object storage is not configured")

        return _fail


def create_app(
    settings: Settings | None = None,
    *,
    authz_resolver: AuthzResolver | None = None,
    enqueue: Any = None,
    store: ObjectStore | None = None,
) -> FastAPI:
    settings = settings or get_settings()

    async def startup(app: FastAPI) -> None:
        app.state.enqueue = enqueue or celery_enqueue
        if store is not None:
            app.state.store = store
        elif not hasattr(app.state, "store"):
            try:
                app.state.store = ObjectStore.from_settings(settings)
            except RuntimeError:
                if settings.is_production_like:
                    raise
                log.warning("object_store_not_configured")  # dev/test without S3: uploads fail and are logged
                app.state.store = _UnconfiguredStore()
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
        app.state.tts = select_tts(settings)

        async def token() -> str:
            return str(await app.state.service_token())

        app.state.gateway = GatewayStream(
            settings.service_url("ai-gateway"), token, timeout=settings.llm_turn_timeout_seconds
        )

    async def shutdown(app: FastAPI) -> None:
        if getattr(app.state, "vault", None) is not None:
            await app.state.vault.close()

    async def sweeper(app: FastAPI) -> None:
        await postprocess_sweep(AppDeps(app))

    async def retention(app: FastAPI) -> None:
        await retention_purge(AppDeps(app))

    return create_service_app(
        settings,
        title="NexusCall Voice / Conversation Service",
        routers=routers,
        outbox=outbox,
        inbox=inbox,
        authz_resolver=authz_resolver,
        on_startup=[startup],
        on_shutdown=[shutdown],
        periodic_jobs=[
            PeriodicJob("voice-postprocess-sweeper", settings.postprocess_interval_seconds, sweeper),
            PeriodicJob("voice-retention", settings.retention_interval_seconds, retention),
        ],
    )


app = create_app()

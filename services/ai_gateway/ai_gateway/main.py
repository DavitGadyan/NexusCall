from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from ai_gateway.api import routers
from ai_gateway.orchestrator import Gateway
from ai_gateway.providers.anthropic_provider import AnthropicProvider
from ai_gateway.providers.embeddings import FakeEmbeddings, VoyageEmbeddings
from ai_gateway.settings import Settings, get_settings
from ai_gateway.tables import inbox, outbox
from nexus_core.app import create_service_app
from nexus_core.clients import service_client
from nexus_core.config import ProviderMode
from nexus_core.credentials import CredentialResolver
from nexus_core.db import Database
from nexus_core.providers import select_adapter
from nexus_core.vault import VaultClient


def create_app(
    settings: Settings | None = None,
    *,
    providers: dict[str, Any] | None = None,
    embeddings: Any = None,
    credentials: Any = None,
) -> FastAPI:
    settings = settings or get_settings()

    async def startup(app: FastAPI) -> None:
        fake = settings.provider_mode is ProviderMode.FAKE
        # In fake mode the REAL adapter talks to the local simulator, so the production code path runs.
        base_url = f"{settings.fake_providers_url.rstrip('/')}/anthropic" if fake else settings.anthropic_base_url
        app.state.providers = providers or {"anthropic": AnthropicProvider(base_url)}
        app.state.embeddings = embeddings or select_adapter(
            settings, real=lambda: VoyageEmbeddings(settings.voyage_base_url), fake=FakeEmbeddings, name="embeddings"
        )
        vault = None
        if not fake and (settings.vault_role_id_file or settings.vault_token_file):
            vault = VaultClient(
                settings.vault_addr,
                kv_mount=settings.vault_kv_mount,
                role_id_file=settings.vault_role_id_file,
                secret_id_file=settings.vault_secret_id_file,
                token_file=settings.vault_token_file,
            )
        app.state.vault = vault
        app.state.credentials = credentials or CredentialResolver(
            settings, vault, None if fake else service_client(app, "integration-service")
        )
        app.state.platform_db = (
            Database(settings.platform_database_url.get_secret_value(), pool_size=2)
            if settings.platform_database_url
            else None
        )
        app.state.gateway = Gateway(app)

    async def shutdown(app: FastAPI) -> None:
        if getattr(app.state, "vault", None):
            await app.state.vault.close()
        if getattr(app.state, "platform_db", None):
            await app.state.platform_db.dispose()

    return create_service_app(
        settings,
        title="NexusCall AI Gateway",
        routers=routers,
        outbox=outbox,
        inbox=inbox,
        on_startup=[startup],
        on_shutdown=[shutdown],
    )


app = create_app()

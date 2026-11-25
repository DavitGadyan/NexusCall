from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from nexus_core.app import create_service_app
from nexus_core.config import ProviderMode
from nexus_core.vault import VaultClient
from nexus_providers.email import EmailProvider
from notification_service.api import routers
from notification_service.consumers import ALLOWED_PRODUCERS, make_handlers
from notification_service.mailer import CredentialSource, Mailer
from notification_service.providers.email import (
    FAKE_CREDENTIALS,
    PlatformSesCredentials,
    StaticCredentials,
    build_email_provider,
)
from notification_service.settings import Settings, get_settings
from notification_service.tables import inbox, outbox


def create_app(
    settings: Settings | None = None,
    *,
    email_provider: EmailProvider | None = None,
    email_credentials: CredentialSource | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    holder: dict[str, Any] = {}

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
        provider = email_provider or build_email_provider(
            settings,
            mailpit_hostport=settings.mailpit_smtp_host,
            configuration_set=settings.ses_configuration_set,
            timeout_seconds=settings.email_send_timeout_seconds,
        )
        if email_credentials is not None:
            creds: CredentialSource = email_credentials
        elif settings.provider_mode is ProviderMode.FAKE:
            creds = StaticCredentials(FAKE_CREDENTIALS)
        else:
            creds = PlatformSesCredentials(vault, settings.env.value)
        app.state.mailer = Mailer(
            provider,
            creds,
            from_address=settings.notification_from_address,
            from_name=settings.notification_from_name,
            public_base_url=settings.public_base_url,
            per_recipient_per_hour=settings.email_per_recipient_per_hour,
        )

    async def shutdown(app: FastAPI) -> None:
        if getattr(app.state, "vault", None) is not None:
            await app.state.vault.close()

    app = create_service_app(
        settings,
        title="NexusCall Notification Service",
        routers=routers,
        outbox=outbox,
        inbox=inbox,
        event_handlers=make_handlers(lambda: getattr(holder["app"].state, "redis", None)),
        allowed_producers=ALLOWED_PRODUCERS,
        on_startup=[startup],
        on_shutdown=[shutdown],
    )
    holder["app"] = app
    return app


app = create_app()

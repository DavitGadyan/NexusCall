from __future__ import annotations

import logging

from fastapi import FastAPI

from nexus_core.app import create_service_app
from nexus_core.config import Environment
from nexus_core.db import Database
from nexus_core.scheduling import PeriodicJob
from nexus_core.vault import VaultClient
from tenant_service.access import LocalAuthzResolver
from tenant_service.api import routers
from tenant_service.idp import FakeIdp, IdpAdmin, KeycloakAdmin
from tenant_service.settings import Settings, get_settings
from tenant_service.tables import inbox, outbox

log = logging.getLogger(__name__)


def _allow_reserved_dev_domains(settings: Settings) -> None:
    """Dev/test identities use RFC 2606/6762 reserved domains (.test, .local); production keeps strict validation."""
    if settings.env in (Environment.DEVELOPMENT, Environment.TEST):
        import email_validator

        for name in ("test", "local"):
            if name in email_validator.SPECIAL_USE_DOMAIN_NAMES:
                email_validator.SPECIAL_USE_DOMAIN_NAMES.remove(name)


def create_app(
    settings: Settings | None = None, *, idp: IdpAdmin | None = None, database: Database | None = None
) -> FastAPI:
    settings = settings or get_settings()
    _allow_reserved_dev_domains(settings)

    async def startup(app: FastAPI) -> None:
        if idp is not None:
            app.state.idp = idp
        elif settings.idp_mode == "fake":
            if settings.env not in (Environment.DEVELOPMENT, Environment.TEST):
                raise RuntimeError("IDP_MODE=fake is only allowed in development/test")
            app.state.idp = FakeIdp()
        else:
            app.state.idp = KeycloakAdmin(
                settings.keycloak_admin_base, settings.keycloak_realm, app.state.service_token
            )
        if settings.vault_role_id_file or settings.vault_token_file:
            app.state.vault = VaultClient(
                settings.vault_addr,
                role_id_file=settings.vault_role_id_file,
                secret_id_file=settings.vault_secret_id_file,
                token_file=settings.vault_token_file,
                kv_mount=settings.vault_kv_mount,
            )
        app.state.authz_resolver = LocalAuthzResolver(app.state.db)

    async def shutdown(app: FastAPI) -> None:
        if getattr(app.state, "vault", None):
            await app.state.vault.close()

    async def expire_grants(app: FastAPI) -> None:
        from tenant_service.api.platform import expire_support_grants

        await expire_support_grants(app.state.db)

    app = create_service_app(
        settings,
        title="NexusCall Tenant & Authorization Service",
        routers=routers,
        outbox=outbox,
        inbox=inbox,
        on_startup=[startup],
        on_shutdown=[shutdown],
        # Auto-revoke expired support grants (build.md §20.3). Idempotent; single runner via Redis lock.
        periodic_jobs=[PeriodicJob("support-grant-expiry", 60, expire_grants)],
    )
    if database is not None:
        app.state.db = database
    return app


app = create_app()

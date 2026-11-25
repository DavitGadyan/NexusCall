from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from nexus_core.app import create_service_app
from nexus_core.authz import AuthzResolver
from nexus_core.scheduling import PeriodicJob
from nexus_core.vault import VaultClient
from tool_execution.api import routers
from tool_execution.approvals import expire_approvals
from tool_execution.executor import Executor
from tool_execution.mcp_client import McpClient
from tool_execution.settings import Settings, get_settings
from tool_execution.tables import inbox, outbox


def create_app(
    settings: Settings | None = None,
    *,
    authz_resolver: AuthzResolver | None = None,
    vault: Any = None,
    mcp_client: McpClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()

    async def startup(app: FastAPI) -> None:
        if vault is not None:
            app.state.vault = vault
        elif settings.vault_role_id_file or settings.vault_token_file:
            # MCP credentials are read under THIS service's Vault policy, at execution time only.
            app.state.vault = VaultClient(
                settings.vault_addr,
                kv_mount=settings.vault_kv_mount,
                role_id_file=settings.vault_role_id_file,
                secret_id_file=settings.vault_secret_id_file,
                token_file=settings.vault_token_file,
            )
        else:
            app.state.vault = None

    async def shutdown(app: FastAPI) -> None:
        if isinstance(getattr(app.state, "vault", None), VaultClient):
            await app.state.vault.close()

    async def expire(app: FastAPI) -> None:
        await expire_approvals(app.state.db)

    app = create_service_app(
        settings,
        title="NexusCall Tool Execution Service",
        routers=routers,
        outbox=outbox,
        inbox=inbox,
        authz_resolver=authz_resolver,
        on_startup=[startup],
        on_shutdown=[shutdown],
        periodic_jobs=[PeriodicJob("approval-expiry", settings.approval_expiry_interval_seconds, expire)],
    )
    app.state.mcp_client = mcp_client or McpClient(settings.mcp_protocol_version)
    app.state.executor = Executor(app)
    return app


app = create_app()

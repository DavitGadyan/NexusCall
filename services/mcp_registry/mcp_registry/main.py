from __future__ import annotations

from fastapi import FastAPI

from mcp_registry.api import routers
from mcp_registry.settings import Settings, get_settings
from mcp_registry.tables import inbox, outbox
from nexus_core.app import create_service_app


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    return create_service_app(
        settings,
        title="NexusCall MCP Registry / Tool Control Plane",
        routers=routers,
        outbox=outbox,
        inbox=inbox,
    )


app = create_app()

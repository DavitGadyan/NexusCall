from __future__ import annotations

from fastapi import FastAPI

from nexus_core.app import create_service_app
from tool_execution.api import routers
from tool_execution.settings import Settings, get_settings
from tool_execution.tables import inbox, outbox


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    return create_service_app(
        settings,
        title="NexusCall Tool Execution Service",
        routers=routers,
        outbox=outbox,
        inbox=inbox,
    )


app = create_app()

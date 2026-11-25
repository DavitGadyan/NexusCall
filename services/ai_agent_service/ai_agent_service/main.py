from __future__ import annotations

from fastapi import FastAPI

from ai_agent_service.api import routers
from ai_agent_service.settings import Settings, get_settings
from ai_agent_service.tables import inbox, outbox
from nexus_core.app import create_service_app


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    return create_service_app(
        settings,
        title="NexusCall AI Agent Service",
        routers=routers,
        outbox=outbox,
        inbox=inbox,
    )


app = create_app()

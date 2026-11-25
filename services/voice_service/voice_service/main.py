from __future__ import annotations

from fastapi import FastAPI

from nexus_core.app import create_service_app
from voice_service.api import routers
from voice_service.settings import Settings, get_settings
from voice_service.tables import inbox, outbox


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    return create_service_app(
        settings,
        title="NexusCall Voice / Conversation Service",
        routers=routers,
        outbox=outbox,
        inbox=inbox,
    )


app = create_app()

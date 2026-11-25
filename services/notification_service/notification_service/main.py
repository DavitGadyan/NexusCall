from __future__ import annotations

from fastapi import FastAPI

from nexus_core.app import create_service_app
from notification_service.api import routers
from notification_service.settings import Settings, get_settings
from notification_service.tables import inbox, outbox


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    return create_service_app(
        settings,
        title="NexusCall Notification Service",
        routers=routers,
        outbox=outbox,
        inbox=inbox,
    )


app = create_app()

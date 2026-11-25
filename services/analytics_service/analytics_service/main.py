from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from analytics_service.api import routers
from analytics_service.api.exports import celery_for
from analytics_service.exports import sweep_exports
from analytics_service.projections import ALLOWED_PRODUCERS, EVENT_HANDLERS
from analytics_service.settings import Settings, get_settings
from analytics_service.tables import inbox, outbox
from nexus_core.app import create_service_app
from nexus_core.db import Database
from nexus_core.scheduling import PeriodicJob
from nexus_core.storage import ObjectStore


def create_app(
    settings: Settings | None = None,
    *,
    platform_database: Database | None = None,
    object_store: ObjectStore | None = None,
    celery: Any = None,
) -> FastAPI:
    settings = settings or get_settings()

    async def startup(app: FastAPI) -> None:
        if platform_database is not None:
            app.state.platform_db = platform_database
        elif settings.platform_database_url is not None:
            # Read-only cross-tenant role with its own SELECT policies (usage totals, export sweeper).
            app.state.platform_db = Database(
                settings.platform_database_url.get_secret_value(), pool_size=2, max_overflow=0
            )
        else:
            app.state.platform_db = None
        app.state.object_store = object_store  # None -> created lazily from S3 settings
        app.state.celery = celery  # None -> analytics_service.worker.celery_app on first use

    async def shutdown(app: FastAPI) -> None:
        if getattr(app.state, "platform_db", None) is not None:
            await app.state.platform_db.dispose()

    async def sweep(app: FastAPI) -> None:
        if app.state.platform_db is None:
            return
        await sweep_exports(
            app.state.db,
            app.state.platform_db,
            celery_for(app),
            stale_queued_seconds=settings.export_stale_queued_seconds,
            stale_processing_seconds=settings.export_stale_processing_seconds,
        )

    return create_service_app(
        settings,
        title="NexusCall Analytics & Reporting Service",
        routers=routers,
        outbox=outbox,
        inbox=inbox,
        event_handlers=EVENT_HANDLERS,
        allowed_producers=ALLOWED_PRODUCERS,
        on_startup=[startup],
        on_shutdown=[shutdown],
        periodic_jobs=[PeriodicJob("export-sweeper", settings.export_sweep_seconds, sweep)],
    )


app = create_app()

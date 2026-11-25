from __future__ import annotations

from fastapi import FastAPI

from audit_service.api import routers
from audit_service.consumer import EVENT_HANDLERS
from audit_service.settings import Settings, get_settings
from audit_service.tables import inbox, outbox
from nexus_core.app import create_service_app
from nexus_core.db import Database
from nexus_core.storage import ObjectStore


def create_app(
    settings: Settings | None = None,
    *,
    platform_database: Database | None = None,
    object_store: ObjectStore | None = None,
) -> FastAPI:
    settings = settings or get_settings()

    async def startup(app: FastAPI) -> None:
        if platform_database is not None:
            app.state.platform_db = platform_database
        elif settings.platform_database_url is not None:
            # Read-only cross-tenant role with its own SELECT policy (never the runtime role).
            app.state.platform_db = Database(
                settings.platform_database_url.get_secret_value(), pool_size=2, max_overflow=0
            )
        app.state.object_store = object_store  # None -> created lazily from S3 settings

    async def shutdown(app: FastAPI) -> None:
        pdb = getattr(app.state, "platform_db", None)
        if pdb is not None:
            await pdb.dispose()

    return create_service_app(
        settings,
        title="NexusCall Audit Service",
        routers=routers,
        outbox=outbox,
        inbox=inbox,
        # Every producer emits `audit.event`; no producer allowlist (the broker restricts publishers).
        event_handlers=EVENT_HANDLERS,
        on_startup=[startup],
        on_shutdown=[shutdown],
    )


app = create_app()

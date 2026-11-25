from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI

from crm_service.api import routers
from crm_service.consumers import ALLOWED_PRODUCERS, HANDLERS
from crm_service.jobs import sweep_stuck_jobs
from crm_service.settings import Settings, get_settings
from crm_service.tables import inbox, outbox
from nexus_core.app import create_service_app
from nexus_core.authz import AuthzResolver
from nexus_core.celery_app import JobContext
from nexus_core.scheduling import PeriodicJob
from nexus_core.storage import ObjectStore

log = logging.getLogger(__name__)


async def _celery_enqueue(name: str, queue: str, ctx: JobContext, **ids: str) -> None:
    from crm_service.worker import celery_app
    from nexus_core.celery_app import enqueue_task

    await enqueue_task(celery_app, name, queue, ctx, **ids)


def create_app(
    settings: Settings | None = None,
    *,
    authz_resolver: AuthzResolver | None = None,
    object_store: ObjectStore | None = None,
    enqueue: Any = None,
) -> FastAPI:
    settings = settings or get_settings()

    async def startup(app: FastAPI) -> None:
        if object_store is not None:
            app.state.object_store = object_store
        elif settings.s3_access_key_id is not None:
            app.state.object_store = ObjectStore.from_settings(settings)
        else:
            app.state.object_store = None
            log.warning("object_store_not_configured")
        app.state.enqueue = enqueue or _celery_enqueue

    async def sweep(app: FastAPI) -> None:
        await sweep_stuck_jobs(app.state.db, app.state.enqueue, stale_seconds=settings.job_stale_seconds)

    return create_service_app(
        settings,
        title="NexusCall CRM Service",
        routers=routers,
        outbox=outbox,
        inbox=inbox,
        event_handlers=HANDLERS,
        allowed_producers=ALLOWED_PRODUCERS,
        authz_resolver=authz_resolver,
        on_startup=[startup],
        periodic_jobs=[PeriodicJob("crm-job-sweeper", settings.sweeper_interval_seconds, sweep)],
    )


app = create_app()

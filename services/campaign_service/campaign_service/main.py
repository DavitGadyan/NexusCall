from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from campaign_service.api import routers
from campaign_service.consumers import ALLOWED_PRODUCERS, HANDLERS
from campaign_service.dispatcher import dispatch_tick
from campaign_service.domain import AppDeps
from campaign_service.settings import Settings, get_settings
from campaign_service.tables import inbox, outbox
from nexus_core.app import create_service_app
from nexus_core.authz import AuthzResolver
from nexus_core.celery_app import JobContext
from nexus_core.scheduling import PeriodicJob


async def _celery_enqueue(name: str, queue: str, ctx: JobContext, **ids: str) -> None:
    from campaign_service.worker import celery_app
    from nexus_core.celery_app import enqueue_task

    await enqueue_task(celery_app, name, queue, ctx, **ids)


def create_app(
    settings: Settings | None = None, *, authz_resolver: AuthzResolver | None = None, enqueue: Any = None
) -> FastAPI:
    settings = settings or get_settings()

    async def startup(app: FastAPI) -> None:
        app.state.enqueue = enqueue or _celery_enqueue

    async def tick(app: FastAPI) -> None:
        await dispatch_tick(
            AppDeps(app),
            batch_max=settings.dispatch_batch_max,
            retry_seconds=settings.transient_retry_seconds,
            snapshot_stale_seconds=settings.snapshot_stale_seconds,
            enqueue=app.state.enqueue,
        )

    return create_service_app(
        settings,
        title="NexusCall Campaign Service",
        routers=routers,
        outbox=outbox,
        inbox=inbox,
        event_handlers=HANDLERS,
        allowed_producers=ALLOWED_PRODUCERS,
        authz_resolver=authz_resolver,
        on_startup=[startup],
        periodic_jobs=[PeriodicJob("campaign-dispatcher", settings.dispatch_interval_seconds, tick)],
    )


app = create_app()

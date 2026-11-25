from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI

from email_inbox_service.api import routers
from email_inbox_service.deps import app_deps
from email_inbox_service.inbound import build_verifier
from email_inbox_service.jobs import retention, sweep
from email_inbox_service.settings import Settings, get_settings
from email_inbox_service.tables import inbox, outbox
from nexus_core.app import create_service_app
from nexus_core.authz import AuthzResolver
from nexus_core.celery_app import JobContext
from nexus_core.scheduling import PeriodicJob
from nexus_core.storage import ObjectStore
from nexus_core.webhooks import SnsVerifier

log = logging.getLogger(__name__)


async def _celery_enqueue(name: str, queue: str, ctx: JobContext, **ids: str) -> None:
    from email_inbox_service.worker import celery_app
    from nexus_core.celery_app import enqueue_task

    await enqueue_task(celery_app, name, queue, ctx, **ids)


def create_app(
    settings: Settings | None = None,
    *,
    authz_resolver: AuthzResolver | None = None,
    object_store: ObjectStore | None = None,
    enqueue: Any = None,
    sns_verifier: SnsVerifier | None = None,
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
        app.state.sns_verifier = sns_verifier or build_verifier(settings)

    async def sweeper(app: FastAPI) -> None:
        await sweep(app_deps(app))

    async def purge(app: FastAPI) -> None:
        await retention(app_deps(app))

    return create_service_app(
        settings,
        title="NexusCall Email Inbox & AI Draft Service",
        routers=routers,
        outbox=outbox,
        inbox=inbox,
        authz_resolver=authz_resolver,
        on_startup=[startup],
        periodic_jobs=[
            PeriodicJob("inbox-sweeper", settings.sweeper_interval_seconds, sweeper),
            PeriodicJob("inbox-retention", settings.retention_interval_seconds, purge),
        ],
    )


app = create_app()

"""Celery worker for call-service queues (latency-sensitive, prefetch 1):

    celery -A call_service.worker worker -Q calls.dispatch,calls.provider_callbacks --concurrency 4

`calls.schedule` belongs to campaign-service and is not consumed here.
"""

from __future__ import annotations

import uuid
from typing import Any

from call_service.deps import WorkerDeps
from call_service.jobs import dispatch_call, hangup_call, process_provider_event, reconcile_call
from call_service.settings import get_settings
from nexus_core.celery_app import JobContext, TaskRuntime, create_celery

celery_app = create_celery(
    "call-service",
    get_settings().amqp_url.get_secret_value(),
    queues=("calls.dispatch", "calls.provider_callbacks"),
)
runtime = TaskRuntime(get_settings)


def _deps() -> WorkerDeps:
    return WorkerDeps(runtime, get_settings())


@celery_app.task(name="calls.dispatch", queue="calls.dispatch", soft_time_limit=50, time_limit=60)
def dispatch(ctx: dict[str, Any], call_id: str) -> None:
    runtime.run(dispatch_call(_deps(), JobContext(**ctx), uuid.UUID(call_id)))


@celery_app.task(name="calls.reconcile", queue="calls.dispatch", soft_time_limit=50, time_limit=60)
def reconcile(ctx: dict[str, Any], call_id: str) -> None:
    runtime.run(reconcile_call(_deps(), JobContext(**ctx), uuid.UUID(call_id)))


@celery_app.task(name="calls.hangup", queue="calls.dispatch", soft_time_limit=25, time_limit=30)
def hangup(ctx: dict[str, Any], call_id: str) -> None:
    runtime.run(hangup_call(_deps(), JobContext(**ctx), uuid.UUID(call_id)))


@celery_app.task(name="calls.provider_callback", queue="calls.provider_callbacks", soft_time_limit=25, time_limit=30)
def provider_callback(ctx: dict[str, Any], event_id: str) -> None:
    runtime.run(process_provider_event(_deps(), JobContext(**ctx), uuid.UUID(event_id)))

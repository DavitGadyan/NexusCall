"""Celery worker for the `analytics.aggregate` queue.

    celery -A analytics_service.worker:celery_app worker -Q analytics.aggregate --concurrency 2

Tasks take `ctx` plus string ids only; the body is the plain coroutine `exports.build_export`.
"""

from __future__ import annotations

import uuid
from typing import Any

from analytics_service.exports import QUEUE, TASK_NAME, build_export
from analytics_service.settings import get_settings
from nexus_core.celery_app import JobContext, TaskRuntime, create_celery

celery_app = create_celery("analytics-service", get_settings().amqp_url.get_secret_value(), queues=(QUEUE,))
runtime = TaskRuntime(get_settings)


@celery_app.task(name=TASK_NAME, queue=QUEUE, time_limit=600, soft_time_limit=540)  # type: ignore[misc]
def build_export_task(ctx: dict[str, Any], export_id: str) -> str:
    settings = get_settings()
    return runtime.run(
        build_export(
            runtime.db,
            runtime.store,
            JobContext(**ctx),
            uuid.UUID(export_id),
            max_rows=settings.export_max_rows,
            max_attempts=settings.export_max_attempts,
        )
    )

"""Celery worker for outbound webhook deliveries (egress-guarded, prefetch 1):

celery -A integration_service.worker worker -Q integrations.webhooks --concurrency 4
"""

from __future__ import annotations

import uuid
from typing import Any

from integration_service.secrets import SecretStore
from integration_service.settings import Settings, get_settings
from integration_service.webhooks import QUEUE, TASK_NAME, DeliveryDeps, deliver
from nexus_core.celery_app import JobContext, TaskRuntime, create_celery, enqueue_task

celery_app = create_celery("integration-service", get_settings().amqp_url.get_secret_value(), queues=(QUEUE,))
runtime = TaskRuntime(get_settings)


def _deps() -> DeliveryDeps:
    settings: Any = runtime.settings
    assert isinstance(settings, Settings)
    return DeliveryDeps(runtime.db, SecretStore(runtime.vault), settings)


@celery_app.task(name=TASK_NAME, queue=QUEUE, soft_time_limit=40, time_limit=60)
def deliver_webhook(ctx: dict[str, Any], delivery_id: str) -> None:
    # Retries are scheduled in the database (next_attempt_at) so they survive restarts; the task never
    # autoretries a POST whose outcome is unknown.
    runtime.run(deliver(_deps(), JobContext(**ctx), uuid.UUID(delivery_id)))


async def publish(ctx: JobContext, delivery_id: str) -> None:
    await enqueue_task(celery_app, TASK_NAME, QUEUE, ctx, delivery_id=delivery_id)

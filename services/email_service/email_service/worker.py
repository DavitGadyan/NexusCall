"""Celery worker for outbound email.

    celery -A email_service.worker worker -Q emails.campaign_send --concurrency 4 --prefetch-multiplier 4

Tasks take `ctx` (JobContext) and the message id only. Both tasks run the same coroutine
(`email_service.sending.send_message`); `emails.send_message` carries reply/transactional mail on
`settings.transactional_queue` (`emails.send_approved` belongs to email-inbox-service's own task).
"""

from __future__ import annotations

import uuid
from typing import Any

from email_service.deps import WorkerDeps
from email_service.sending import send_message
from email_service.settings import get_settings
from nexus_core.celery_app import JobContext, TaskRuntime, create_celery

_settings = get_settings()
_queues = tuple(dict.fromkeys(("emails.campaign_send", _settings.transactional_queue)))
celery_app = create_celery("email-service", _settings.amqp_url.get_secret_value(), queues=_queues)
runtime = TaskRuntime(get_settings)
deps = WorkerDeps(runtime)


@celery_app.task(name="emails.campaign_send", queue="emails.campaign_send", soft_time_limit=60, time_limit=90)
def campaign_send(ctx: dict[str, Any], message_id: str) -> str:
    return runtime.run(send_message(deps, JobContext(**ctx), uuid.UUID(message_id))).status


@celery_app.task(name="emails.send_message", queue=_settings.transactional_queue, soft_time_limit=60, time_limit=90)
def send_one(ctx: dict[str, Any], message_id: str) -> str:
    return runtime.run(send_message(deps, JobContext(**ctx), uuid.UUID(message_id))).status

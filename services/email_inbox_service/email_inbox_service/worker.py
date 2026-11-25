"""Celery workers for the inbox queues.

    celery -A email_inbox_service.worker worker -Q emails.inbound_process --concurrency 4
    celery -A email_inbox_service.worker worker -Q emails.ai_draft --concurrency 2
    celery -A email_inbox_service.worker worker -Q emails.send_approved --concurrency 2

Tasks take `ctx` plus string ids only. The coroutines live in inbound/drafting/sending and are tested directly.
"""

from __future__ import annotations

import uuid
from typing import Any

from email_inbox_service.deps import Deps
from email_inbox_service.drafting import generate_draft
from email_inbox_service.inbound import process_inbound_safe
from email_inbox_service.sending import send_approved
from email_inbox_service.settings import (
    DRAFT_QUEUE,
    DRAFT_TASK,
    INBOUND_QUEUE,
    INBOUND_TASK,
    SEND_QUEUE,
    SEND_TASK,
    Settings,
    get_settings,
)
from nexus_core.celery_app import JobContext, TaskRuntime, create_celery, enqueue_task

celery_app = create_celery(
    "email-inbox-service", get_settings().amqp_url.get_secret_value(), queues=(INBOUND_QUEUE, DRAFT_QUEUE, SEND_QUEUE)
)
runtime = TaskRuntime(get_settings)


async def _enqueue(name: str, queue: str, ctx: JobContext, **ids: str) -> None:
    await enqueue_task(celery_app, name, queue, ctx, **ids)


def _deps() -> Deps:
    settings = runtime.settings
    assert isinstance(settings, Settings)
    return Deps(
        settings=settings,
        db=runtime.db,
        redis=runtime.redis,
        store=runtime.store,
        client=lambda name: runtime.client(name, timeout=15.0),
        enqueue=_enqueue,
    )


@celery_app.task(name=INBOUND_TASK, queue=INBOUND_QUEUE, soft_time_limit=240, time_limit=300)
def inbound_process(ctx: dict[str, Any], event_id: str) -> None:
    runtime.run(process_inbound_safe(_deps(), JobContext(**ctx), uuid.UUID(event_id)))


@celery_app.task(name=DRAFT_TASK, queue=DRAFT_QUEUE, soft_time_limit=200, time_limit=240)
def ai_draft(ctx: dict[str, Any], draft_id: str) -> None:
    runtime.run(generate_draft(_deps(), JobContext(**ctx), uuid.UUID(draft_id)))


@celery_app.task(name=SEND_TASK, queue=SEND_QUEUE, soft_time_limit=60, time_limit=90, max_retries=8)
def send_approved_task(ctx: dict[str, Any], command_id: str) -> None:
    runtime.run(send_approved(_deps(), JobContext(**ctx), uuid.UUID(command_id)))

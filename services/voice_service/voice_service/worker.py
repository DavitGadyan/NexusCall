"""Celery worker for `voice.postprocess`:

celery -A voice_service.worker worker -Q voice.postprocess --concurrency 2
"""

from __future__ import annotations

import uuid
from typing import Any

from nexus_core.celery_app import JobContext, TaskRuntime, create_celery
from voice_service.deps import WorkerDeps
from voice_service.jobs import postprocess_session
from voice_service.settings import get_settings

celery_app = create_celery("voice-service", get_settings().amqp_url.get_secret_value(), queues=("voice.postprocess",))
runtime = TaskRuntime(get_settings)


@celery_app.task(name="voice.postprocess", queue="voice.postprocess", soft_time_limit=100, time_limit=120)
def postprocess(ctx: dict[str, Any], session_id: str) -> None:
    runtime.run(postprocess_session(WorkerDeps(runtime, get_settings()), JobContext(**ctx), uuid.UUID(session_id)))

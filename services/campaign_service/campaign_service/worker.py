"""Celery worker for the `calls.schedule` queue (audience snapshots).

    celery -A campaign_service.worker worker -Q calls.schedule --concurrency 2

call-service's worker must not consume `calls.schedule` (it owns calls.dispatch / calls.provider_callbacks).
"""

from __future__ import annotations

import uuid
from typing import Any

from campaign_service.settings import get_settings
from campaign_service.snapshot import snapshot_audience
from nexus_core.celery_app import JobContext, TaskRuntime, create_celery

celery_app = create_celery("campaign-service", get_settings().amqp_url.get_secret_value(), queues=("calls.schedule",))
runtime = TaskRuntime(get_settings)


@celery_app.task(name="campaigns.snapshot", queue="calls.schedule", soft_time_limit=540, time_limit=600)
def campaigns_snapshot(ctx: dict[str, Any], campaign_id: str) -> None:
    s = get_settings()
    runtime.run(
        snapshot_audience(
            runtime,
            JobContext(**ctx),
            uuid.UUID(campaign_id),
            page_size=s.snapshot_page_size,
            stale_seconds=s.snapshot_stale_seconds,
        )
    )

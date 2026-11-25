"""Celery worker for the `imports.process` queue (imports and lead exports).

celery -A crm_service.worker worker -Q imports.process --concurrency 2
"""

from __future__ import annotations

import uuid
from typing import Any

from crm_service.jobs import build_export, process_import
from crm_service.settings import get_settings
from nexus_core.celery_app import JobContext, TaskRuntime, create_celery

celery_app = create_celery("crm-service", get_settings().amqp_url.get_secret_value(), queues=("imports.process",))
runtime = TaskRuntime(get_settings)


@celery_app.task(name="imports.process", queue="imports.process", soft_time_limit=840, time_limit=900)
def imports_process(ctx: dict[str, Any], import_id: str) -> None:
    s = get_settings()
    runtime.run(
        process_import(
            runtime,
            JobContext(**ctx),
            uuid.UUID(import_id),
            chunk_rows=s.import_chunk_rows,
            max_rows=s.import_max_rows,
            max_bytes=s.import_max_bytes,
            stale_seconds=s.job_stale_seconds,
        )
    )


@celery_app.task(name="exports.build", queue="imports.process", soft_time_limit=540, time_limit=600)
def exports_build(ctx: dict[str, Any], export_id: str) -> None:
    s = get_settings()
    runtime.run(
        build_export(
            runtime,
            JobContext(**ctx),
            uuid.UUID(export_id),
            max_rows=s.export_max_rows,
            stale_seconds=s.job_stale_seconds,
        )
    )

"""Celery worker for the `ai.knowledge_ingest` queue (ingestion, re-embedding, evaluation runs).

celery -A ai_agent_service.worker worker -Q ai.knowledge_ingest --concurrency 2
"""

from __future__ import annotations

import uuid
from typing import Any

from ai_agent_service.evals import run_evaluation
from ai_agent_service.knowledge import ingest_document, reembed_tenant
from ai_agent_service.settings import INGEST_QUEUE, get_settings
from nexus_core.celery_app import JobContext, TaskRuntime, create_celery

celery_app = create_celery("ai-agent-service", get_settings().amqp_url.get_secret_value(), queues=(INGEST_QUEUE,))
runtime = TaskRuntime(get_settings)


@celery_app.task(name="ai.knowledge_ingest", queue=INGEST_QUEUE, soft_time_limit=840, time_limit=900)
def knowledge_ingest(ctx: dict[str, Any], document_id: str) -> None:
    runtime.run(ingest_document(runtime, JobContext(**ctx), uuid.UUID(document_id)))


@celery_app.task(name="ai.knowledge_reembed", queue=INGEST_QUEUE, soft_time_limit=1740, time_limit=1800)
def knowledge_reembed(ctx: dict[str, Any], job_id: str) -> None:
    runtime.run(reembed_tenant(runtime, JobContext(**ctx), uuid.UUID(job_id)))


@celery_app.task(name="ai.eval_run", queue=INGEST_QUEUE, soft_time_limit=1740, time_limit=1800)
def eval_run(ctx: dict[str, Any], run_id: str) -> None:
    runtime.run(run_evaluation(runtime, JobContext(**ctx), uuid.UUID(run_id)))

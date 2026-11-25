"""Celery factory and task conventions (ADR-0011).

Every task receives a serialized `JobContext` (tenant, correlation, actor, job id)
and only identifiers. Tasks raise `PermanentFailure` for errors that must not be
retried (invalid recipient, permission denied, opt-out) and `TransientFailure`
for retryable ones; retries use exponential backoff with jitter.
"""

from __future__ import annotations

import uuid
from typing import Any

from celery import Celery, Task
from kombu import Exchange, Queue  # type: ignore[attr-defined]
from pydantic import BaseModel, ConfigDict, Field

from nexus_core.context import correlation_id_var, tenant_id_var

QUEUES = (
    "calls.schedule",
    "calls.dispatch",
    "calls.provider_callbacks",
    "voice.postprocess",
    "emails.campaign_send",
    "emails.inbound_process",
    "emails.ai_draft",
    "emails.send_approved",
    "integrations.webhooks",
    "imports.process",
    "analytics.aggregate",
    "notifications.send",
)
LATENCY_SENSITIVE = frozenset({"calls.dispatch", "calls.provider_callbacks", "emails.send_approved"})


class PermanentFailure(Exception):
    """Do not retry. The task records the failure in the owning DB and exits."""


class TransientFailure(Exception):
    """Retry with backoff."""


class JobContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    job_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    correlation_id: uuid.UUID
    causation_id: uuid.UUID | None = None
    actor_type: str = "system"
    actor_id: str = "system"


class NexusTask(Task):  # type: ignore[misc]
    autoretry_for = (TransientFailure, ConnectionError, TimeoutError)
    dont_autoretry_for = (PermanentFailure,)
    retry_backoff = 2
    retry_backoff_max = 300
    retry_jitter = True
    max_retries = 5
    acks_late = True
    reject_on_worker_lost = True
    ignore_result = True

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        ctx = kwargs.get("ctx")
        if isinstance(ctx, dict):
            correlation_id_var.set(str(ctx.get("correlation_id")))
            tenant_id_var.set(str(ctx.get("tenant_id")))
        return super().__call__(*args, **kwargs)


def create_celery(name: str, broker_url: str, *, queues: tuple[str, ...] = QUEUES) -> Celery:
    app = Celery(name, broker=broker_url, task_cls=NexusTask)
    tasks_exchange = Exchange("nexus.tasks", type="direct", durable=True)  # type: ignore[no-untyped-call]
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_backend=None,
        task_ignore_result=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        broker_transport_options={"confirm_publish": True},
        broker_connection_retry_on_startup=True,
        task_default_exchange="nexus.tasks",
        task_default_delivery_mode="persistent",
        task_time_limit=300,
        task_soft_time_limit=240,
        worker_hijack_root_logger=False,
        worker_enable_remote_control=False,
        worker_send_task_events=False,
        task_send_sent_event=False,
        task_queues=[
            Queue(  # type: ignore[no-untyped-call]
                q,
                tasks_exchange,
                routing_key=q,
                durable=True,
                queue_arguments={
                    "x-queue-type": "quorum",
                    "x-dead-letter-exchange": "nexus.dlx",
                    "x-dead-letter-routing-key": f"{q}.dlq",
                },
            )
            for q in queues
        ],
    )
    return app

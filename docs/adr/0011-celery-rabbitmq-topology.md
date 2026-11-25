# ADR-0011: Celery on RabbitMQ — topology and task rules

- Status: Accepted
- Date: 2026-11-25

## Decision
**Broker layout**
- One vhost, `nexus`, with a broker user per service and per worker. Permissions use per-user regexes on exchanges and queues.
- The queues listed in build.md §6.1 are quorum queues, each with `x-dead-letter-exchange=nexus.dlx` → `<queue>.dlq`.

**Celery settings**
- `task_acks_late=True`
- `task_reject_on_worker_lost=True`
- `worker_prefetch_multiplier=1` for latency-sensitive queues, 4 for bulk queues
- `broker_transport_options={"confirm_publish": True}`
- `task_time_limit` and `task_soft_time_limit` set per task
- `autoretry_for` transient errors, with `retry_backoff=True`, `retry_jitter=True` and `max_retries` set per task
- Permanent errors raise `PermanentFailure`, which is never retried
- No result backend for business state (`task_ignore_result=True` by default)

**Workers and scheduling**
- One container per workload group (SERVICE_MAP §4).
- Exactly one `celery-beat`, holding a Redis lock (`redbeat`) so a second replica idles.

**Every task**
- Accepts a `JobContext(tenant_id, correlation_id, causation_id, actor, job_id)`.
- Opens `tenant_session` for each unit of work.
- Is idempotent through domain uniqueness and inbox rows.

**DLQ handling**
- The DLQ replay tool lives in the platform admin UI, Phase 7. It requires `platform:queues:replay`, is audited, and re-publishes with a new `causation_id`.

## Consequences
- Duplicate-delivery, worker-crash and DLQ behaviour are covered by integration tests against a real RabbitMQ via testcontainers.

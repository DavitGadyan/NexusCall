# ADR-0014: Periodic jobs run in-process with a distributed lock (no celery-beat)

- Status: Accepted
- Date: 2026-11-25
- Amends: ADR-0011 (the "exactly one celery-beat" part)

## Context
Many services need small periodic jobs: expiry of invitations and support grants, campaign dispatch ticks, reconciliation of ambiguous provider calls, quota resets, retention purges, sweepers that re-enqueue stuck jobs. A dedicated celery-beat per service would add 16 more containers. A single shared beat would need to import every service's task modules, which couples the services.

build.md §9.1 allows either "only one active scheduler per deployment" or "distributed locking".

## Decision
- Each service declares `PeriodicJob`s (`nexus_core.scheduling`). They run as supervised asyncio tasks inside the API process.
- Before every tick a job takes a Redis lock (`SET NX EX`), so exactly one replica runs it. The lock is released with a compare-and-delete script.
- Jobs must be idempotent, because a lock can expire if a run takes too long.
- Heavy work still goes to Celery queues. A periodic job only selects rows and enqueues IDs.
- Outbox relays and event consumers also run in-process (see ARCHITECTURE §7). `FOR UPDATE SKIP LOCKED` and the inbox table make them safe with several replicas.

## Consequences
- Fewer containers, and no extra moving part to keep single-active.
- If Redis is down, periodic jobs pause (they fail closed). Business state stays in PostgreSQL, and jobs resume when Redis returns.
- Celery workers remain separate containers per workload (ADR-0011 unchanged there).

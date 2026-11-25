"""Periodic jobs inside a service process, single-active across replicas via a Redis lock (ADR-0014).

build.md §9.1 allows "a dedicated scheduler ... or distributed locking"; this avoids a separate
celery-beat container per service while guaranteeing one runner per tick. Jobs must be idempotent:
a lock can expire if a run takes longer than `lock_seconds`.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


@dataclass(frozen=True, slots=True)
class PeriodicJob:
    name: str
    interval_seconds: float
    func: Callable[[Any], Awaitable[None]]  # receives the FastAPI app
    lock: bool = True
    lock_seconds: int | None = None
    jitter: float = 0.1


async def run_once_locked(app: Any, job: PeriodicJob) -> bool:
    """Run the job if this process wins the lock. Returns True when the job ran."""
    redis = getattr(app.state, "redis", None)
    service = app.state.settings.service_name
    key, token = f"lock:periodic:{service}:{job.name}", str(uuid.uuid4())
    if job.lock and redis is not None:
        ttl = job.lock_seconds or max(int(job.interval_seconds * 3), 30)
        if not await redis.set(key, token, nx=True, ex=ttl):
            return False
    try:
        await job.func(app)
        return True
    finally:
        if job.lock and redis is not None:
            try:
                await redis.eval(_RELEASE, 1, key, token)
            except Exception:  # noqa: BLE001 - lock expires on its own
                log.warning("periodic_lock_release_failed", extra={"job": job.name})


async def run_periodic(app: Any, job: PeriodicJob) -> None:
    while True:
        delay = job.interval_seconds * (1 + random.uniform(-job.jitter, job.jitter))  # noqa: S311 - jitter only
        await asyncio.sleep(max(delay, 0.05))
        try:
            await run_once_locked(app, job)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("periodic_job_failed", extra={"job": job.name})

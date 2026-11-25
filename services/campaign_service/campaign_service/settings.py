from __future__ import annotations

from functools import lru_cache

from nexus_core.config import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "campaign-service"
    # Dispatcher (PeriodicJob, Redis-locked so one replica runs each tick).
    dispatch_interval_seconds: float = 3.0
    launches_per_hour: int = 20  # per tenant (build.md §5.5)
    dispatch_batch_max: int = 50  # contacts per campaign per tick
    transient_retry_seconds: int = 30  # call-service/crm unavailable: same attempt (same idempotency key) later
    # Audience snapshot (calls.schedule task).
    snapshot_page_size: int = 500
    snapshot_stale_seconds: int = 300


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

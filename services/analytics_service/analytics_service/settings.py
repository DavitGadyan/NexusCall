from __future__ import annotations

from functools import lru_cache

from nexus_core.config import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "analytics-service"
    # Email events carry no cost: an estimate per accepted email is used (SES list price $0.10 / 1000).
    email_unit_cost_micros: int = 100
    max_range_days: int = 366
    export_url_seconds: int = 300
    export_max_rows: int = 100_000
    export_max_attempts: int = 3
    export_sweep_seconds: float = 60.0
    export_stale_queued_seconds: int = 120
    export_stale_processing_seconds: int = 900


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

from __future__ import annotations

from functools import lru_cache

from nexus_core.config import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "audit-service"
    # Exports are generated synchronously and therefore bounded; larger ranges must be split by date.
    audit_export_max_rows: int = 10_000
    audit_export_url_seconds: int = 300
    verify_batch_size: int = 1_000


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

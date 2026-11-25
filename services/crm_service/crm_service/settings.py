from __future__ import annotations

from functools import lru_cache

from nexus_core.config import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "crm-service"
    # CSV imports are up to 10 MB; allow multipart overhead. JSON bodies are bounded by their models.
    max_request_body_bytes: int = 11 * 1024 * 1024
    import_max_bytes: int = 10 * 1024 * 1024
    import_max_rows: int = 50_000
    import_chunk_rows: int = 500
    export_max_rows: int = 100_000
    # Background jobs left `queued`/`processing` longer than this are re-enqueued by the sweeper.
    job_stale_seconds: int = 600
    sweeper_interval_seconds: float = 60.0
    presign_seconds: int = 300


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

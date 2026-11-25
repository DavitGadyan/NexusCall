from __future__ import annotations

from functools import lru_cache

from nexus_core.config import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "tenant-service"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

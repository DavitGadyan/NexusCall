from __future__ import annotations

from functools import lru_cache

from nexus_core.config import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "booking-service"
    # Only tool-execution calls the booking internal API (BUILD_CONVENTIONS §5).
    internal_client_allowlist: tuple[str, ...] = ("svc-tool-execution",)

    # Tentative holds created by AI agents expire unless confirmed (build.md §3.1J).
    tentative_hold_minutes: int = 15
    # Reminders are requested this long before a confirmed appointment starts.
    reminder_lead_minutes: int = 24 * 60
    max_availability_days: int = 31
    max_slots: int = 200

    # Google Calendar REST API (real mode). In fake mode the same adapter talks to the simulator.
    google_calendar_api_base: str = "https://www.googleapis.com/calendar/v3"
    google_token_url: str = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint URL, not a secret
    calendar_timeout_seconds: float = 8.0
    calendar_sync_batch: int = 20
    calendar_sync_max_attempts: int = 8


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

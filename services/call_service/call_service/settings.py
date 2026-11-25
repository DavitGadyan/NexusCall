from __future__ import annotations

from functools import lru_cache
from typing import Self

from pydantic import SecretStr, model_validator

from nexus_core.config import ServiceSettings

# Development/test only fallback so a laptop stack works without extra setup. Production must set
# STREAM_TOKEN_SECRET (delivered by Vault Agent); the validator below refuses to start otherwise.
_DEV_STREAM_SECRET = "dev-only-stream-token-secret-change-me"  # noqa: S105


class Settings(ServiceSettings):
    service_name: str = "call-service"

    # Allow-listed callers of /internal endpoints (BUILD_CONVENTIONS §5, call-service).
    campaign_clients: tuple[str, ...] = ("svc-campaign-service",)
    transfer_clients: tuple[str, ...] = ("svc-tool-execution", "svc-voice-service")
    read_clients: tuple[str, ...] = ("svc-voice-service", "svc-tool-execution", "svc-ai-agent-service")
    voice_clients: tuple[str, ...] = ("svc-voice-service",)

    # Public URLs. Twilio signs the exact URL it calls, so these must match what Twilio was given.
    webhook_base_url: str = "http://localhost:47000"
    media_stream_url: str = "wss://localhost:47000/media/twilio"
    twilio_api_base: str = "https://api.twilio.com"
    twilio_timeout_seconds: float = 10.0

    # Shared with voice-service only: signs the short-lived media stream token (Vault in production).
    stream_token_secret: SecretStr | None = None
    stream_token_ttl_seconds: int = 120

    # Dialing policy
    ring_timeout_seconds: int = 30
    max_dial_attempts: int = 3
    reconcile_after_seconds: int = 30
    concurrency_retry_seconds: int = 30
    live_call_max_seconds: int = 3600
    sweeper_interval_seconds: float = 15.0
    reconcile_interval_seconds: float = 30.0
    stale_queued_seconds: int = 30

    @model_validator(mode="after")
    def _require_stream_secret(self) -> Self:
        if self.is_production_like and self.stream_token_secret is None:
            raise ValueError("STREAM_TOKEN_SECRET is required outside development/test")
        return self

    def stream_secret(self) -> bytes:
        if self.stream_token_secret is not None:
            return self.stream_token_secret.get_secret_value().encode()
        return _DEV_STREAM_SECRET.encode()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

from __future__ import annotations

from functools import lru_cache
from typing import Self

from pydantic import SecretStr, model_validator

from nexus_core.config import ServiceSettings

_DEV_STREAM_SECRET = "dev-only-stream-token-secret-change-me"  # noqa: S105 - dev/test fallback only


class Settings(ServiceSettings):
    service_name: str = "voice-service"

    # Shared with call-service only (Vault in production): verifies the media stream token.
    stream_token_secret: SecretStr | None = None

    # Providers (endpoints are configuration, never request input).
    deepgram_ws_base: str = "wss://api.deepgram.com"
    deepgram_model: str = "nova-3"
    endpointing_ms: int = 300
    elevenlabs_base: str = "https://api.elevenlabs.io"
    elevenlabs_model: str = "eleven_flash_v2_5"
    default_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    provider_timeout_seconds: float = 10.0

    # Conversation limits
    start_timeout_seconds: float = 10.0
    no_speech_timeout_seconds: float = 12.0
    no_speech_reprompts: int = 1
    max_call_seconds: int = 1800
    llm_turn_timeout_seconds: float = 12.0
    llm_max_tokens: int = 300
    max_history_turns: int = 16
    barge_in_min_chars: int = 3
    data_classification: str = "confidential"

    # Storage and access
    max_recording_seconds: int = 1800
    presign_seconds: int = 300
    retention_interval_seconds: float = 3600.0
    postprocess_interval_seconds: float = 60.0

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

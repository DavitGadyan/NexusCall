from __future__ import annotations

from functools import lru_cache

from nexus_core.config import ServiceSettings

# Hard per-purpose output ceilings (LLM06). Callers may ask for less, never more.
PURPOSE_MAX_TOKENS: dict[str, int] = {
    "voice_turn": 400,
    "classification": 512,
    "summary": 1500,
    "email_draft": 2500,
    "call_postprocess": 2000,
    "eval": 4000,
}
PURPOSE_DEADLINE_SECONDS: dict[str, float] = {
    "voice_turn": 12.0,
    "classification": 30.0,
    "summary": 60.0,
    "email_draft": 90.0,
    "call_postprocess": 90.0,
    "eval": 120.0,
}


class Settings(ServiceSettings):
    service_name: str = "ai-gateway"
    # Only these services may call the gateway (separate identity per caller; LLM03/LLM08).
    internal_client_allowlist: tuple[str, ...] = (
        "svc-voice-service",
        "svc-ai-agent-service",
        "svc-email-inbox-service",
        "svc-call-service",
    )
    # Provider endpoints are configuration, never request input (LLM04 / SSRF).
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_timeout_seconds: float = 60.0
    voyage_base_url: str = "https://api.voyageai.com"
    embedding_model: str = "voyage-3"
    embedding_dims: int = 1024

    # Unbounded consumption limits (LLM06)
    max_messages: int = 60
    max_blocks: int = 200
    max_input_chars: int = 200_000
    max_system_chars: int = 30_000
    max_untrusted_block_chars: int = 50_000
    max_tool_rounds: int = 4
    max_tool_calls_per_round: int = 4
    max_tool_calls_total: int = 8
    max_tool_result_chars: int = 8_000
    max_repeated_tool_call: int = 2
    max_embedding_texts: int = 64
    max_embedding_chars: int = 8_000
    tenant_requests_per_minute: int = 600
    agent_requests_per_minute: int = 300
    tenant_max_concurrent: int = 40
    default_request_cost_cap_micros: int = 500_000  # $0.50 per request unless a policy says otherwise
    schema_repair_attempts: int = 1

    # Data minimization (LLM02): untrusted content at these classifications is PII-redacted before
    # it reaches an external model provider.
    pii_redact_classifications: tuple[str, ...] = ("restricted",)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

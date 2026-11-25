"""Approved model catalogue seeded by migration (OWASP LLM04: only registered, enabled models run).

Prices are micros of USD per million tokens (first-party Claude API list prices, 2026-10). Platform
admins can disable models or change prices in the console; requests can never name an unregistered model.
"""

from __future__ import annotations

ALL_CLASSES = ["public", "internal", "confidential", "restricted"]

MODELS: list[dict[str, object]] = [
    {
        "model_id": "claude-haiku-5-5",
        "provider": "anthropic",
        "display_name": "Claude Haiku 5.5",
        "input_price_micros_per_mtok": 100_000,
        "output_price_micros_per_mtok": 500_000,
        "max_output_tokens": 8_000,
        "allowed_classifications": ALL_CLASSES,
        "purposes": ["voice_turn", "classification", "summary"],
    },
    {
        "model_id": "claude-sonnet-5-5",
        "provider": "anthropic",
        "display_name": "Claude Sonnet 5.5",
        "input_price_micros_per_mtok": 2_000_000,
        "output_price_micros_per_mtok": 10_000_000,
        "max_output_tokens": 16_000,
        "allowed_classifications": ALL_CLASSES,
        "purposes": ["email_draft", "summary", "classification", "call_postprocess", "voice_turn"],
    },
    {
        "model_id": "claude-opus-5-5",
        "provider": "anthropic",
        "display_name": "Claude Opus 5.5",
        "input_price_micros_per_mtok": 4_000_000,
        "output_price_micros_per_mtok": 20_000_000,
        "max_output_tokens": 16_000,
        "allowed_classifications": ALL_CLASSES,
        "purposes": ["eval", "email_draft", "call_postprocess", "summary"],
    },
    {
        "model_id": "voyage-3",
        "provider": "voyage",
        "display_name": "Voyage 3 (embeddings)",
        "input_price_micros_per_mtok": 60_000,
        "output_price_micros_per_mtok": 0,
        "max_output_tokens": 0,
        "allowed_classifications": ["public", "internal", "confidential"],
        "purposes": ["embedding"],
    },
]

ROUTING: list[dict[str, object]] = [
    {"purpose": "voice_turn", "models": ["claude-haiku-5-5", "claude-sonnet-5-5"], "failover_allowed": True},
    {"purpose": "classification", "models": ["claude-haiku-5-5", "claude-sonnet-5-5"], "failover_allowed": True},
    {"purpose": "summary", "models": ["claude-sonnet-5-5", "claude-haiku-5-5"], "failover_allowed": True},
    {"purpose": "email_draft", "models": ["claude-sonnet-5-5", "claude-opus-5-5"], "failover_allowed": True},
    {"purpose": "call_postprocess", "models": ["claude-sonnet-5-5"], "failover_allowed": False},
    {"purpose": "eval", "models": ["claude-opus-5-5"], "failover_allowed": False},
    {"purpose": "embedding", "models": ["voyage-3"], "failover_allowed": False},
]

CLASS_RANK = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


def cost_micros(input_tokens: int, output_tokens: int, in_price: int, out_price: int) -> int:
    """Integer micros, rounded up so budgets are never under-counted."""
    return -(-(input_tokens * in_price + output_tokens * out_price) // 1_000_000)

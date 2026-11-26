"""Code-defined provider catalogue: the secret fields stored at `vault_ref` (BUILD_CONVENTIONS §5) with their
shape checks, the non-secret config keys a tenant may set, and how each provider is masked.

Validation errors name the field and the rule, never the submitted value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import SecretStr

from nexus_core.errors import Unprocessable

_TOKENISH = re.compile(r"^[A-Za-z0-9._\-~+/=:]{16,512}$")


@dataclass(frozen=True, slots=True)
class FieldSpec:
    pattern: re.Pattern[str]
    rule: str


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    key: str
    fields: dict[str, FieldSpec]
    config_keys: frozenset[str] = frozenset()
    oauth: bool = False
    # Services allowed to own a platform-level default credential for this provider.
    platform_services: frozenset[str] = frozenset()
    optional_fields: frozenset[str] = field(default_factory=frozenset)
    platform_only: bool = False


def _f(pattern: str, rule: str) -> FieldSpec:
    return FieldSpec(re.compile(pattern), rule)


TOKEN = FieldSpec(_TOKENISH, "16-512 URL-safe characters")
PROVIDERS: dict[str, ProviderSpec] = {
    "twilio": ProviderSpec(
        "twilio",
        {"account_sid": _f(r"^AC[0-9a-fA-F]{32}$", "AC followed by 32 hex characters"),
         "auth_token": _f(r"^[0-9a-fA-F]{32}$", "32 hex characters")},
        platform_services=frozenset({"call-service", "voice-service"}),
    ),
    "ses": ProviderSpec(
        "ses",
        {"region": _f(r"^[a-z]{2}(-[a-z]+)+-\d$", "an AWS region such as us-east-1"),
         "access_key_id": _f(r"^(AKIA|ASIA)[A-Z0-9]{16}$", "AKIA/ASIA followed by 16 characters"),
         "secret_access_key": _f(r"^[A-Za-z0-9/+=]{40}$", "40 base64 characters")},
        platform_services=frozenset({"email-service", "notification-service", "email-inbox-service"}),
    ),
    "anthropic": ProviderSpec(
        "anthropic", {"api_key": _f(r"^sk-ant-[A-Za-z0-9_\-]{20,200}$", "an Anthropic key (sk-ant-…)")},
        platform_services=frozenset({"ai-gateway"}),
    ),
    "deepgram": ProviderSpec("deepgram", {"api_key": _f(r"^[A-Za-z0-9]{32,64}$", "32-64 alphanumeric characters")},
                             platform_services=frozenset({"voice-service"})),
    "elevenlabs": ProviderSpec("elevenlabs", {"api_key": _f(r"^(sk_)?[A-Za-z0-9]{32,64}$", "an ElevenLabs API key")},
                               platform_services=frozenset({"voice-service"})),
    "voyage": ProviderSpec("voyage", {"api_key": _f(r"^pa-[A-Za-z0-9_\-]{20,200}$", "a Voyage key (pa-…)")},
                           platform_services=frozenset({"ai-gateway"})),
    "google_calendar": ProviderSpec(
        "google_calendar",
        {"client_id": _f(r"^[A-Za-z0-9.\-_]{10,200}$", "an OAuth client id"),
         "client_secret": TOKEN,
         "refresh_token": TOKEN},
        config_keys=frozenset({"calendar_id"}),
        oauth=True,
    ),
    "hubspot": ProviderSpec(
        "hubspot",
        {"access_token": TOKEN},
        oauth=True,
        # OAuth connections additionally keep what is needed to refresh the short-lived access token.
        optional_fields=frozenset({"refresh_token", "client_id", "client_secret"}),
    ),
    # The platform's own OAuth app registrations (used to run the tenant authorization-code flows).
    "google_oauth_client": ProviderSpec(
        "google_oauth_client",
        {"client_id": _f(r"^[A-Za-z0-9.\-_]{10,200}$", "an OAuth client id"), "client_secret": TOKEN},
        platform_services=frozenset({"integration-service"}), platform_only=True,
    ),
    "hubspot_oauth_client": ProviderSpec(
        "hubspot_oauth_client",
        {"client_id": _f(r"^[A-Za-z0-9\-]{8,64}$", "a HubSpot app client id"), "client_secret": TOKEN},
        platform_services=frozenset({"integration-service"}), platform_only=True,
    ),
}  # fmt: skip
OAUTH_CLIENT = {"google_calendar": "google_oauth_client", "hubspot": "hubspot_oauth_client"}


def spec_for(provider: str) -> ProviderSpec:
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise Unprocessable("Unknown provider", extra={"providers": sorted(PROVIDERS)})
    return spec


def validate_credentials(provider: str, values: dict[str, SecretStr]) -> dict[str, SecretStr]:
    spec = spec_for(provider)
    allowed = set(spec.fields) | set(spec.optional_fields)
    unknown = sorted(set(values) - allowed)
    missing = sorted(set(spec.fields) - set(values))
    errors = [{"field": k, "rule": "not a field of this provider"} for k in unknown]
    errors += [{"field": k, "rule": "required"} for k in missing]
    for name, rule in spec.fields.items():
        if name in values and not rule.pattern.fullmatch(values[name].get_secret_value()):
            errors.append({"field": name, "rule": rule.rule})
    for name in spec.optional_fields & set(values):
        if not TOKEN.pattern.fullmatch(values[name].get_secret_value()):
            errors.append({"field": name, "rule": TOKEN.rule})
    if errors:
        raise Unprocessable("Credential fields are invalid", extra={"fields": errors})
    return values


def validate_config(provider: str, config: dict[str, str]) -> dict[str, str]:
    spec = spec_for(provider)
    unknown = sorted(set(config) - spec.config_keys)
    if unknown:
        raise Unprocessable("Unknown configuration keys", extra={"fields": unknown})
    for key, value in config.items():
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9@._\-]{1,255}", value):
            raise Unprocessable("Invalid configuration value", extra={"fields": [key]})
    return config


def mask(value: str, *, keep: int = 4) -> str:
    """Show the provider's identifier shape without revealing it: `AC…3f2a`, `d…@example.com`."""
    if "@" in value:
        local, _, domain = value.partition("@")
        return f"{local[:1]}…@{domain}"
    if len(value) <= keep + 2:
        return "…" + value[-2:]
    return f"{value[:2]}…{value[-keep:]}"

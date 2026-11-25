"""Structured JSON logging with redaction (build.md §10.1).

Redaction is applied to the message and to every extra field. It removes values
under sensitive keys and masks email addresses, phone numbers, bearer tokens and
JWT-like strings. Logging full message bodies or prompts is never done by
library code; services must log identifiers instead.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from nexus_core.context import actor_var, correlation_id_var, request_id_var, tenant_id_var

SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "authorization",
        "api_key",
        "apikey",
        "client_secret",
        "private_key",
        "cookie",
        "set-cookie",
        "auth_token",
        "secret_id",
        "credentials",
        "body",
        "html",
        "text_body",
        "prompt",
        "completion",
        "transcript",
        "phone",
        "email",
    }
)
REDACTED = "[REDACTED]"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)bearer\s+[a-z0-9\-._~+/]+=*"), "Bearer [REDACTED]"),
    (re.compile(r"eyJ[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}"), "[JWT]"),
    (re.compile(r"(?i)\b(hvs|hvb|s)\.[A-Za-z0-9]{20,}"), "[VAULT_TOKEN]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[API_KEY]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    (re.compile(r"\+?\d[\d\s().-]{8,}\d"), "[PHONE]"),
)


def redact_text(value: str) -> str:
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def redact(value: Any, key: str | None = None) -> Any:
    if key is not None and key.lower() in SENSITIVE_KEYS:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [redact(v) for v in value]
    return value


_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, env: str) -> None:
        super().__init__()
        self.service = service
        self.env = env

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "service": self.service,
            "env": self.env,
            "msg": redact_text(record.getMessage()),
            "request_id": request_id_var.get(),
            "correlation_id": correlation_id_var.get(),
            "tenant_id": tenant_id_var.get(),
            "actor": actor_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = redact(value, key)
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(service: str, env: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service, env))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).setLevel(logging.WARNING)

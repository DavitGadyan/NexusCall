from __future__ import annotations

import hashlib
import secrets


def new_invitation_token() -> tuple[str, str]:
    """Returns (raw_token, sha256_hex). 256 bits of entropy; only the hash is stored (build.md §18.3)."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"

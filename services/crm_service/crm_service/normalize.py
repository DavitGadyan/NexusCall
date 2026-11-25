"""Normalization, hashing and masking of contact addresses (emails, E.164 phone numbers)."""

from __future__ import annotations

import hashlib
import re
import uuid

import phonenumbers

# Deliberately strict and simple: one @, no whitespace or control characters, a dotted domain.
_EMAIL = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


class InvalidAddress(ValueError):
    """Raised with a short, PII-free reason code."""


def normalize_email(raw: str) -> str:
    value = raw.strip().lower()
    if len(value) > 320 or not _EMAIL.match(value) or ".." in value:
        raise InvalidAddress("invalid_email")
    return value


def normalize_phone(raw: str, region: str | None = "US") -> str:
    """Parse to E.164 with libphonenumber. A leading + ignores `region`."""
    value = raw.strip()
    if not value or len(value) > 32:
        raise InvalidAddress("invalid_phone")
    try:
        parsed = phonenumbers.parse(value, None if value.startswith("+") else (region or "US"))
    except phonenumbers.NumberParseException as exc:
        raise InvalidAddress("invalid_phone") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise InvalidAddress("invalid_phone")
    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    if not _E164.match(e164):
        raise InvalidAddress("invalid_phone")
    return e164


def phone_country(e164: str) -> str | None:
    try:
        return phonenumbers.region_code_for_number(phonenumbers.parse(e164, None)) or None
    except phonenumbers.NumberParseException:
        return None


def value_hash(value: str) -> str:
    """sha256 of the already-normalized value (email-service hashes the same way)."""
    return hashlib.sha256(value.encode()).hexdigest()


def contact_marker_hash(contact_id: uuid.UUID) -> str:
    """Hash used for suppressions that name a contact but no address."""
    return value_hash(f"contact:{contact_id}")


def normalize_for_channel(channel: str, raw: str, region: str | None = "US") -> str:
    if channel == "email":
        return normalize_email(raw)
    if channel == "phone":
        return normalize_phone(raw, region)
    raise InvalidAddress("invalid_channel")


def mask_email(email: str | None) -> str | None:
    if not email:
        return None
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    return f"{phone[:2]}{'*' * max(len(phone) - 6, 0)}{phone[-4:]}"

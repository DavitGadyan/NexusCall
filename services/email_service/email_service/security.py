"""Address normalization, hashing, tokens and header safety (build.md §4 "abuse controls")."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid

_LOCAL = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
DOMAIN_RE = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(\.{_LABEL})+$")
ADDRESS_RE = re.compile(rf"^{_LOCAL}@{_LABEL}(\.{_LABEL})+$")
MSG_ID_RE = re.compile(r"^<[\x21-\x3b\x3d\x3f-\x7e]{1,250}@[\x21-\x3b\x3d\x3f-\x7e]{1,250}>$")


class UnsafeValue(ValueError):
    pass


def normalize_address(value: str) -> str:
    """Trimmed, lower-cased address (the same normalization crm uses for `address_hash`)."""
    return value.strip().lower()


def address_hash(value: str) -> str:
    return hashlib.sha256(normalize_address(value).encode()).hexdigest()


def valid_address(value: str) -> bool:
    return len(value) <= 320 and ADDRESS_RE.match(value) is not None and not has_crlf(value)


def domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower()


def normalize_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    if not DOMAIN_RE.match(domain) or not domain.isascii():
        raise UnsafeValue("Invalid domain name")
    return domain


def has_crlf(value: str) -> bool:
    """Header injection guard: CR, LF and other C0 controls (except TAB) are never allowed in headers."""
    return any(ch in value for ch in ("\r", "\n", "\x00")) or any(ord(c) < 32 and c != "\t" for c in value)


def safe_header(value: str, *, max_len: int = 998) -> str:
    if has_crlf(value) or len(value) > max_len:
        raise UnsafeValue("Header values must be a single line")
    return value


def valid_message_id_list(value: str) -> bool:
    """`In-Reply-To` / `References`: one or more `<id@host>` tokens separated by spaces."""
    if has_crlf(value) or len(value) > 4000:
        return False
    parts = value.split()
    return bool(parts) and len(parts) <= 50 and all(MSG_ID_RE.match(p) for p in parts)


def new_token() -> tuple[str, str]:
    """(raw, sha256 hex). 256 bits of entropy; only the hash is stored."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_rfc_message_id(domain: str) -> str:
    return f"<{uuid.uuid4().hex}@{domain}>"


def mask_address(value: str | None) -> str | None:
    if not value:
        return None
    local, _, domain = value.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"

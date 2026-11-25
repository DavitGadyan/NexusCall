"""Webhook authenticity: NexusCall HMAC signatures, Twilio signatures, AWS SNS signatures.

All comparisons are constant-time. Timestamps bound replay windows; event-ID
dedupe (inbox / provider_events unique constraints) handles in-window replays.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from collections.abc import Mapping
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

DEFAULT_TOLERANCE_SECONDS = 300


class WebhookVerificationError(Exception):
    pass


# ---- NexusCall outbound/inbound generic HMAC (header: "t=<unix>,v1=<hex>") ----


def sign_payload(secret: bytes, body: bytes, timestamp: int | None = None) -> str:
    ts = int(timestamp if timestamp is not None else time.time())
    mac = hmac.new(secret, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify_signature(
    secret: bytes, body: bytes, header: str, *, tolerance: int = DEFAULT_TOLERANCE_SECONDS, now: float | None = None
) -> int:
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        ts = int(parts["t"])
        provided = parts["v1"]
    except (ValueError, KeyError) as exc:
        raise WebhookVerificationError("Malformed signature header") from exc
    if abs((now or time.time()) - ts) > tolerance:
        raise WebhookVerificationError("Signature timestamp outside tolerance (possible replay)")
    expected = hmac.new(secret, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise WebhookVerificationError("Signature mismatch")
    return ts


# ---- Twilio: HMAC-SHA1 over full URL + sorted POST params, base64 ----


def twilio_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def verify_twilio(auth_token: str, url: str, params: Mapping[str, str], signature: str | None) -> None:
    if not signature:
        raise WebhookVerificationError("Missing X-Twilio-Signature")
    if not hmac.compare_digest(twilio_signature(auth_token, url, params), signature):
        raise WebhookVerificationError("Twilio signature mismatch")


# ---- AWS SNS: certificate from an allow-listed host, SignatureVersion 1 (SHA1) / 2 (SHA256) ----

_SNS_KEYS = {
    "Notification": ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"),
    "SubscriptionConfirmation": ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"),
    "UnsubscribeConfirmation": ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"),
}


def sns_string_to_sign(message: Mapping[str, str]) -> bytes:
    keys = _SNS_KEYS.get(message.get("Type", ""))
    if keys is None:
        raise WebhookVerificationError("Unknown SNS message type")
    out = []
    for key in keys:
        if key in message and message[key] is not None:
            out.append(f"{key}\n{message[key]}\n")
    return "".join(out).encode()


SNS_CERT_HOST_PATTERN = r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$"


def _allowed_cert_url(url: str, host_pattern: str, *, allow_http: bool = False) -> bool:
    """The signing certificate must come from an exact, anchored host pattern over HTTPS.

    (A suffix check would accept look-alike hosts such as `evilamazonaws.com`.)"""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    scheme_ok = parts.scheme == "https" or (allow_http and parts.scheme == "http")
    return (
        scheme_ok
        and not parts.username
        and not parts.password
        and parts.path.endswith(".pem")
        and re.fullmatch(host_pattern, host) is not None
    )


class SnsVerifier:
    def __init__(
        self,
        cert_host_pattern: str = SNS_CERT_HOST_PATTERN,
        allowed_topic_arns: frozenset[str] | None = None,
        *,
        verify_tls: bool = True,
        allow_http: bool = False,
    ) -> None:
        """`allow_http` exists only for the local fake SNS simulator; never enable it with real AWS."""
        self._host_pattern = cert_host_pattern
        self._topics = allowed_topic_arns
        self._certs: dict[str, rsa.RSAPublicKey] = {}
        self._verify_tls = verify_tls
        self._allow_http = allow_http

    async def _public_key(self, url: str) -> rsa.RSAPublicKey:
        if url not in self._certs:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=False, verify=self._verify_tls) as c:
                resp = await c.get(url)
                resp.raise_for_status()
            key = x509.load_pem_x509_certificate(resp.content).public_key()
            if not isinstance(key, rsa.RSAPublicKey):
                raise WebhookVerificationError("Unexpected SNS certificate key type")
            self._certs[url] = key
        return self._certs[url]

    async def verify(self, message: Mapping[str, str], *, tolerance: int = 3600) -> None:
        cert_url = message.get("SigningCertURL") or message.get("SigningCertUrl") or ""
        if not _allowed_cert_url(cert_url, self._host_pattern, allow_http=self._allow_http):
            raise WebhookVerificationError("SNS signing certificate URL is not allowed")
        if self._topics is not None and message.get("TopicArn") not in self._topics:
            raise WebhookVerificationError("SNS topic is not allowed")
        version = message.get("SignatureVersion")
        algo: hashes.HashAlgorithm
        if version == "1":
            algo = hashes.SHA1()  # noqa: S303 - mandated by SNS SignatureVersion 1
        elif version == "2":
            algo = hashes.SHA256()
        else:
            raise WebhookVerificationError("Unsupported SNS signature version")
        try:
            signature = base64.b64decode(message.get("Signature", ""))
        except ValueError as exc:
            raise WebhookVerificationError("Malformed SNS signature") from exc
        key = await self._public_key(cert_url)
        try:
            key.verify(signature, sns_string_to_sign(message), padding.PKCS1v15(), algo)
        except InvalidSignature as exc:
            raise WebhookVerificationError("SNS signature invalid") from exc
        from datetime import datetime

        try:
            ts = datetime.fromisoformat(message["Timestamp"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError) as exc:
            raise WebhookVerificationError("SNS timestamp missing") from exc
        if abs(time.time() - ts) > tolerance:
            raise WebhookVerificationError("SNS message too old (possible replay)")

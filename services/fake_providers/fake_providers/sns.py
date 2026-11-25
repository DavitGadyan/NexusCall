"""DEVELOPMENT/TEST ONLY: Amazon SNS message signing for the simulators.

Generates an ephemeral RSA key and self-signed certificate per process. Services running in fake
mode verify these messages with `SnsVerifier(cert_host_pattern=r"^fake-providers$", allow_http=True)`.
"""

from __future__ import annotations

import base64
import datetime as dt
import os
import uuid
from functools import lru_cache

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from nexus_core.webhooks import sns_string_to_sign

CERT_PATH = "/sns/cert.pem"


@lru_cache(maxsize=1)
def _material() -> tuple[rsa.RSAPrivateKey, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-providers SNS (dev only)")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    return key, cert.public_bytes(serialization.Encoding.PEM)


def certificate_pem() -> bytes:
    return _material()[1]


def public_key() -> rsa.RSAPublicKey:
    return _material()[0].public_key()


def cert_url() -> str:
    base = os.environ.get("FAKE_PROVIDERS_SELF_URL", "http://fake-providers:8000").rstrip("/")
    return f"{base}{CERT_PATH}"


def sign_sns(
    topic_arn: str, message: str, *, subject: str | None = None, message_type: str = "Notification"
) -> dict[str, str]:
    """Build a signed SNS envelope (SignatureVersion 2 / SHA256) around `message` (a JSON string)."""
    envelope: dict[str, str] = {
        "Type": message_type,
        "MessageId": str(uuid.uuid4()),
        "TopicArn": topic_arn,
        "Message": message,
        "Timestamp": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "SignatureVersion": "2",
        "SigningCertURL": cert_url(),
    }
    if subject:
        envelope["Subject"] = subject
    signature = _material()[0].sign(sns_string_to_sign(envelope), padding.PKCS1v15(), hashes.SHA256())
    envelope["Signature"] = base64.b64encode(signature).decode()
    return envelope

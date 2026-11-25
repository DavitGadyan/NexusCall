import base64
import datetime as dt
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from nexus_core.config import Environment, ProviderMode, ServiceSettings
from nexus_core.idempotency import canonical_hash
from nexus_core.rls import tenant_rls_statements
from nexus_core.ssrf import UnsafeDestination, is_blocked_ip, validate_outbound_url
from nexus_core.webhooks import (
    SnsVerifier,
    WebhookVerificationError,
    sign_payload,
    sns_string_to_sign,
    twilio_signature,
    verify_signature,
    verify_twilio,
)


def test_canonical_hash_is_order_independent():
    assert canonical_hash({"a": 1, "b": [1, 2]}) == canonical_hash({"b": [1, 2], "a": 1})
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})


def test_fake_providers_forbidden_in_production():
    with pytest.raises(ValueError, match="forbidden"):
        ServiceSettings(env=Environment.PRODUCTION, provider_mode=ProviderMode.FAKE)
    assert ServiceSettings(env=Environment.DEVELOPMENT, provider_mode=ProviderMode.FAKE)


def test_rls_statements_force_and_cover_all_commands():
    sql = " ".join(tenant_rls_statements("contacts", "crm_app"))
    assert "FORCE ROW LEVEL SECURITY" in sql
    for cmd in ("FOR SELECT", "FOR INSERT", "FOR UPDATE", "FOR DELETE"):
        assert cmd in sql
    with pytest.raises(ValueError):
        tenant_rls_statements("contacts; drop table x", "crm_app")


@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.1.2.3", "169.254.169.254", "192.168.1.1", "::1", "fd00::1", "::ffff:127.0.0.1", "100.64.0.1"],
)
def test_blocked_ips(ip):
    assert is_blocked_ip(ip)


def test_public_ip_allowed():
    assert not is_blocked_ip("93.184.216.34")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:pw@example.com",
        "https://127.0.0.1/x",
        "https://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "https://localhost/",
    ],
)
async def test_ssrf_rejects(url):
    with pytest.raises(UnsafeDestination):
        await validate_outbound_url(url)


async def test_ssrf_allowlist():
    with pytest.raises(UnsafeDestination):
        await validate_outbound_url("https://evil.example", allowed_hosts=frozenset({"api.good.example"}))


def test_hmac_roundtrip_and_replay():
    secret, body = b"s3cret", b'{"a":1}'
    header = sign_payload(secret, body)
    assert verify_signature(secret, body, header)
    with pytest.raises(WebhookVerificationError):
        verify_signature(secret, b'{"a":2}', header)
    old = sign_payload(secret, body, timestamp=int(time.time()) - 3600)
    with pytest.raises(WebhookVerificationError, match="replay"):
        verify_signature(secret, body, old)
    with pytest.raises(WebhookVerificationError):
        verify_signature(secret, body, "garbage")


def test_twilio_signature_algorithm():
    import hashlib
    import hmac as _hmac

    params = {"To": "+18005551212", "CallSid": "CA123", "From": "+12349013030"}
    url = "https://api.example.com/webhooks/twilio/status?x=1"
    # Independent computation of Twilio's documented scheme: URL + params sorted by key, HMAC-SHA1, base64.
    expected = base64.b64encode(
        _hmac.new(b"12345", (url + "CallSidCA123From+12349013030To+18005551212").encode(), hashlib.sha1).digest()
    ).decode()
    assert twilio_signature("12345", url, params) == expected
    verify_twilio("12345", url, params, expected)
    with pytest.raises(WebhookVerificationError):
        verify_twilio("wrong", url, params, expected)
    with pytest.raises(WebhookVerificationError):
        verify_twilio("12345", url, {**params, "To": "+19995550000"}, expected)
    with pytest.raises(WebhookVerificationError):
        verify_twilio("12345", url, params, None)


def _self_signed():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.test")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return key, cert.public_bytes(serialization.Encoding.PEM)


async def test_sns_verification(monkeypatch):
    key, pem = _self_signed()
    msg = {
        "Type": "Notification",
        "MessageId": "m1",
        "TopicArn": "arn:aws:sns:us-east-1:1:t",
        "Message": "{}",
        "Timestamp": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "SignatureVersion": "2",
        "SigningCertURL": "https://sns.us-east-1.amazonaws.com/cert.pem",
    }
    msg["Signature"] = base64.b64encode(key.sign(sns_string_to_sign(msg), padding.PKCS1v15(), hashes.SHA256())).decode()
    verifier = SnsVerifier(allowed_topic_arns=frozenset({"arn:aws:sns:us-east-1:1:t"}))
    verifier._certs[msg["SigningCertURL"]] = x509.load_pem_x509_certificate(pem).public_key()
    await verifier.verify(msg)

    tampered = {**msg, "Message": '{"x":1}'}
    with pytest.raises(WebhookVerificationError):
        await verifier.verify(tampered)
    evil_cert = {**msg, "SigningCertURL": "https://evil.example.com/cert.pem"}
    with pytest.raises(WebhookVerificationError, match="certificate URL"):
        await verifier.verify(evil_cert)
    wrong_topic = {**msg, "TopicArn": "arn:aws:sns:us-east-1:1:other"}
    with pytest.raises(WebhookVerificationError, match="topic"):
        await verifier.verify(wrong_topic)

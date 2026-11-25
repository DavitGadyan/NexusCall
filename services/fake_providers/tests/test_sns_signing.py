import pytest

from fake_providers.sns import cert_url, public_key, sign_sns
from nexus_core.webhooks import SnsVerifier, WebhookVerificationError


async def test_fake_sns_messages_verify_only_with_dev_settings():
    msg = sign_sns("arn:aws:sns:us-east-1:000000000000:nexus-dev", '{"eventType":"Delivery"}')
    dev = SnsVerifier(cert_host_pattern=r"^fake-providers$", allow_http=True, verify_tls=False)
    dev._certs[cert_url()] = public_key()
    await dev.verify(msg)
    with pytest.raises(WebhookVerificationError):
        await dev.verify({**msg, "Message": '{"eventType":"Bounce"}'})
    # A production-configured verifier must reject the simulator outright.
    with pytest.raises(WebhookVerificationError, match="certificate URL"):
        await SnsVerifier().verify(msg)


def test_cert_endpoint(monkeypatch):
    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    from fastapi.testclient import TestClient

    from fake_providers.main import create_app

    with TestClient(create_app()) as c:
        r = c.get("/sns/cert.pem")
        assert r.status_code == 200 and b"BEGIN CERTIFICATE" in r.content

"""Unit tests for pure helpers (no database)."""

from __future__ import annotations

import httpx
import pytest
import respx
from pydantic import SecretStr

from integration_service.providers.catalogue import mask, validate_config, validate_credentials
from integration_service.providers.testers import ProviderTester, _sigv4_headers
from integration_service.settings import Settings
from integration_service.webhooks import backoff_seconds, expand_event_types, pinned_url
from nexus_core.errors import Unprocessable
from nexus_core.ssrf import SafeDestination


def _s(**v: str) -> dict[str, SecretStr]:
    return {k: SecretStr(x) for k, x in v.items()}


def test_credential_shapes():
    validate_credentials("twilio", _s(account_sid="AC" + "a" * 32, auth_token="b" * 32))
    with pytest.raises(Unprocessable) as exc:
        validate_credentials("twilio", _s(account_sid="AC1", extra="x"))
    fields = {f["field"] for f in exc.value.extra["fields"]}
    assert fields == {"account_sid", "auth_token", "extra"}
    assert "AC1" not in str(exc.value.extra)
    validate_credentials("hubspot", _s(access_token="pat-na1-" + "x" * 30))
    validate_config("google_calendar", {"calendar_id": "primary"})
    with pytest.raises(Unprocessable):
        validate_config("twilio", {"calendar_id": "x"})
    with pytest.raises(Unprocessable):
        validate_config("google_calendar", {"calendar_id": "a b/../c"})


def test_masking_never_shows_the_middle():
    assert mask("AC" + "0" * 28 + "3f2a") == "AC…3f2a"
    assert mask("dana@example.com") == "d…@example.com"
    assert mask("12345") == "…45"


def test_event_type_expansion():
    assert expand_event_types(["booking.*", "lead.created"]) == [
        "booking.canceled", "booking.confirmed", "booking.created", "lead.created",
    ]  # fmt: skip
    for bad in (["audit.event"], ["nope.*"], ["integration.connection.changed"]):
        with pytest.raises(Unprocessable):
            expand_event_types(bad)


def test_pinned_url_targets_the_validated_ip():
    v4 = SafeDestination("https://hooks.example.com:8443/a?b=1", "hooks.example.com", 8443, ("93.184.216.34",))
    assert pinned_url(v4) == ("https://93.184.216.34:8443/a?b=1", "hooks.example.com")
    v6 = SafeDestination("https://h.example.com/x", "h.example.com", 443, ("2606:2800:220:1::1",))
    assert pinned_url(v6)[0] == "https://[2606:2800:220:1::1]/x"


def test_backoff_is_bounded_and_jittered():
    s = Settings(env="test", webhook_backoff_base_seconds=30, webhook_backoff_max_seconds=600)
    values = [backoff_seconds(s, n) for n in range(1, 12)]
    assert all(0 < v <= 600 for v in values)
    assert 15 <= backoff_seconds(s, 1) <= 30


def test_sigv4_headers_have_the_documented_shape():
    h = _sigv4_headers("us-east-1", "AKIAEXAMPLE000000000", "secret" * 6, "email.us-east-1.amazonaws.com", "/v2/x")
    assert h["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE000000000/")
    assert "/us-east-1/ses/aws4_request" in h["Authorization"] and "secret" not in h["Authorization"]


async def test_real_testers_map_provider_errors_to_codes():
    t = ProviderTester(Settings(env="test", provider_mode="real"))
    key = _s(api_key="sk-ant-" + "x" * 30)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get("https://api.anthropic.com/v1/models")
        for status, code in ((401, "invalid_credentials"), (503, "provider_unreachable"), (404, "provider_rejected")):
            route.mock(return_value=httpx.Response(status, text="echo sk-ant-xxxx"))
            result = await t.test("anthropic", key, {})
            assert (result.ok, result.error) == (False, code)
        route.mock(side_effect=httpx.ConnectTimeout("x"))
        assert (await t.test("anthropic", key, {})).error == "provider_unreachable"
        route.mock(return_value=httpx.Response(200, json={"data": []}))
        assert (await t.test("anthropic", key, {})).ok
        assert route.calls.last.request.headers["x-api-key"].startswith("sk-ant-")

"""Server-side connection tests against each provider's documented API (build.md §19.3 "validate/test").

Each test makes one cheap, read-only call with the candidate credential held in memory. Results carry an
error *code* only: provider response bodies are never logged or returned (they can echo credentials).

Real endpoints:
  twilio      GET  https://api.twilio.com/2010-04-01/Accounts/{sid}.json            (HTTP Basic)
  ses         GET  https://email.{region}.amazonaws.com/v2/email/account           (SigV4, service "ses")
  anthropic   GET  https://api.anthropic.com/v1/models                             (x-api-key)
  deepgram    GET  https://api.deepgram.com/v1/projects                            (Token)
  elevenlabs  GET  https://api.elevenlabs.io/v1/user                               (xi-api-key)
  voyage      POST https://api.voyageai.com/v1/embeddings                          (Bearer, 1 tiny input)
  google      POST https://oauth2.googleapis.com/token (refresh) + GET calendar/v3/calendars/primary
  hubspot     GET  https://api.hubapi.com/account-info/v3/details                  (Bearer)

Fake mode (development/test only): Google Calendar and HubSpot run against the fake-providers simulators;
the other providers are checked in-process (a value containing "invalid" fails), deterministically.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import SecretStr

from nexus_core.config import ProviderMode, ServiceSettings

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint URL
GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
HUBSPOT_API = "https://api.hubapi.com"


@dataclass(frozen=True, slots=True)
class TestResult:
    ok: bool
    error: str | None = None
    account_id: str | None = None  # raw provider account identifier (masked before storage)
    scopes: list[str] = field(default_factory=list)


class _Fail(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _check(resp: httpx.Response) -> None:
    if resp.status_code in (401, 403):
        raise _Fail("invalid_credentials")
    if resp.status_code == 429 or resp.status_code >= 500:
        raise _Fail("provider_unreachable")
    if resp.status_code >= 300:
        raise _Fail("provider_rejected")


def google_urls(settings: ServiceSettings) -> tuple[str, str]:
    if settings.provider_mode is ProviderMode.FAKE:
        base = settings.fake_providers_url.rstrip("/")
        return f"{base}/google_calendar/token", f"{base}/google_calendar/calendar/v3"
    return GOOGLE_TOKEN_URL, GOOGLE_CALENDAR_API


def hubspot_api(settings: ServiceSettings) -> str:
    if settings.provider_mode is ProviderMode.FAKE:
        return f"{settings.fake_providers_url.rstrip('/')}/hubspot"
    return HUBSPOT_API


def _sigv4_headers(region: str, key_id: str, secret: str, host: str, path: str) -> dict[str, str]:
    """AWS Signature Version 4 for an empty-body GET (https://docs.aws.amazon.com/IAM/latest/UserGuide/)."""
    now = dt.datetime.now(dt.UTC)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical = f"GET\n{path}\n\nhost:{host}\nx-amz-date:{amz_date}\n\nhost;x-amz-date\n{payload_hash}"
    scope = f"{day}/{region}/ses/aws4_request"
    to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    key = f"AWS4{secret}".encode()
    for part in (day, region, "ses", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "x-amz-date": amz_date,
        "Authorization": f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, SignedHeaders=host;x-amz-date, "
        f"Signature={signature}",
    }


class ProviderTester:
    def __init__(self, settings: ServiceSettings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport
        self.timeout = float(getattr(settings, "provider_test_timeout_seconds", 8.0))

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout, transport=self.transport, follow_redirects=False)

    async def test(self, provider: str, values: dict[str, SecretStr], config: dict[str, str]) -> TestResult:
        fake = self.settings.provider_mode is ProviderMode.FAKE
        try:
            if fake and provider not in ("google_calendar", "hubspot"):
                return self._fake(provider, values)
            return await getattr(self, f"_{provider}")(values, config)
        except _Fail as f:
            return TestResult(False, f.code)
        except httpx.HTTPError:
            return TestResult(False, "provider_unreachable")
        except (KeyError, ValueError):
            return TestResult(False, "provider_response_invalid")

    def _fake(self, provider: str, values: dict[str, SecretStr]) -> TestResult:
        if any("invalid" in v.get_secret_value() for v in values.values()):
            return TestResult(False, "invalid_credentials")
        account = None
        if provider == "twilio":
            account = values["account_sid"].get_secret_value()
        elif provider.endswith("_oauth_client"):
            account = values["client_id"].get_secret_value()
        return TestResult(True, account_id=account)

    async def _google_oauth_client(self, v: dict[str, SecretStr], _: dict[str, str]) -> TestResult:
        # An OAuth client can only be exercised by a user authorization flow; the shape was validated.
        return TestResult(True, account_id=v["client_id"].get_secret_value())

    _hubspot_oauth_client = _google_oauth_client

    async def _twilio(self, v: dict[str, SecretStr], _: dict[str, str]) -> TestResult:
        sid = v["account_sid"].get_secret_value()
        async with self._client() as c:
            resp = await c.get(f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json",
                               auth=(sid, v["auth_token"].get_secret_value()))  # fmt: skip
        _check(resp)
        if resp.json().get("status") not in ("active", None):
            raise _Fail("account_not_active")
        return TestResult(True, account_id=sid)

    async def _ses(self, v: dict[str, SecretStr], _: dict[str, str]) -> TestResult:
        region = v["region"].get_secret_value()
        host, path = f"email.{region}.amazonaws.com", "/v2/email/account"
        key_id = v["access_key_id"].get_secret_value()
        headers = _sigv4_headers(region, key_id, v["secret_access_key"].get_secret_value(), host, path)
        async with self._client() as c:
            resp = await c.get(f"https://{host}{path}", headers=headers)
        _check(resp)
        return TestResult(True, account_id=key_id)

    async def _anthropic(self, v: dict[str, SecretStr], _: dict[str, str]) -> TestResult:
        async with self._client() as c:
            resp = await c.get("https://api.anthropic.com/v1/models", params={"limit": 1},
                               headers={"x-api-key": v["api_key"].get_secret_value(),
                                        "anthropic-version": "2023-06-01"})  # fmt: skip
        _check(resp)
        return TestResult(True)

    async def _deepgram(self, v: dict[str, SecretStr], _: dict[str, str]) -> TestResult:
        async with self._client() as c:
            resp = await c.get("https://api.deepgram.com/v1/projects",
                               headers={"Authorization": f"Token {v['api_key'].get_secret_value()}"})  # fmt: skip
        _check(resp)
        projects = resp.json().get("projects") or []
        return TestResult(True, account_id=str(projects[0]["project_id"]) if projects else None)

    async def _elevenlabs(self, v: dict[str, SecretStr], _: dict[str, str]) -> TestResult:
        async with self._client() as c:
            resp = await c.get("https://api.elevenlabs.io/v1/user",
                               headers={"xi-api-key": v["api_key"].get_secret_value()})  # fmt: skip
        _check(resp)
        user = resp.json().get("user_id")
        return TestResult(True, account_id=str(user) if user else None)

    async def _voyage(self, v: dict[str, SecretStr], _: dict[str, str]) -> TestResult:
        async with self._client() as c:
            resp = await c.post("https://api.voyageai.com/v1/embeddings",
                                headers={"Authorization": f"Bearer {v['api_key'].get_secret_value()}"},
                                json={"input": ["connection test"], "model": "voyage-3-lite"})  # fmt: skip
        _check(resp)
        return TestResult(True)

    async def google_access_token(self, v: dict[str, SecretStr]) -> tuple[str, list[str]]:
        token_url, _ = google_urls(self.settings)
        form = {
            "grant_type": "refresh_token",
            "client_id": v["client_id"].get_secret_value(),
            "client_secret": v["client_secret"].get_secret_value(),
            "refresh_token": v["refresh_token"].get_secret_value(),
        }
        async with self._client() as c:
            resp = await c.post(token_url, data=form)
        form.clear()
        if resp.status_code in (400, 401):
            raise _Fail("invalid_credentials")
        _check(resp)
        data: dict[str, Any] = resp.json()
        return str(data["access_token"]), str(data.get("scope", "")).split()

    async def _google_calendar(self, v: dict[str, SecretStr], config: dict[str, str]) -> TestResult:
        token, scopes = await self.google_access_token(v)
        _, api = google_urls(self.settings)
        async with self._client() as c:
            resp = await c.get(f"{api}/calendars/primary", headers={"Authorization": f"Bearer {token}"})
        _check(resp)
        return TestResult(True, account_id=str(resp.json().get("id") or ""), scopes=scopes)

    async def _hubspot(self, v: dict[str, SecretStr], _: dict[str, str]) -> TestResult:
        async with self._client() as c:
            resp = await c.get(f"{hubspot_api(self.settings)}/account-info/v3/details",
                               headers={"Authorization": f"Bearer {v['access_token'].get_secret_value()}"})  # fmt: skip
        _check(resp)
        portal = resp.json().get("portalId")
        return TestResult(True, account_id=str(portal) if portal is not None else None)

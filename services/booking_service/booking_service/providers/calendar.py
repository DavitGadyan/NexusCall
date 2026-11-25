"""Google Calendar adapter (implements `nexus_providers.calendar.CalendarProvider`).

Real mode talks to the documented REST API (https://developers.google.com/calendar/api/v3/reference):
`POST /freeBusy`, `POST /calendars/{id}/events`, `GET|DELETE /calendars/{id}/events/{eventId}`.
Fake mode uses the very same client against the fake-providers simulator (`/google_calendar/...`), so the
contract tests exercise one code path.

External writes are idempotent: the event id is derived from the idempotency key, so a retried insert returns
409 for an event that already exists. After an ambiguous failure the caller reconciles with `event_exists`
before trying again. Errors never carry response bodies (they may echo tokens or attendee data).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from nexus_core.config import ProviderMode, ServiceSettings
from nexus_core.credentials import CredentialResolver, ResolvedCredentials
from nexus_core.providers import select_adapter
from nexus_providers.calendar import BusyInterval, CalendarEvent

PROVIDER = "google_calendar"


class CalendarError(Exception):
    code = "calendar_error"


class CalendarAuthError(CalendarError):
    """Credentials rejected (401/403, invalid_grant). Permanent until the connection is fixed."""

    code = "calendar_auth_failed"


class CalendarRejected(CalendarError):
    """The provider refused the request (4xx). Permanent."""

    code = "calendar_rejected"


class CalendarUnavailable(CalendarError):
    """Timeout, connection failure, 429 or 5xx. The outcome of a write is unknown: reconcile before retrying."""

    code = "calendar_unavailable"


def _q(value: str) -> str:
    return quote(value, safe="")


def event_id_from_key(idempotency_key: str) -> str:
    """Google event ids use base32hex (a-v, 0-9). Keys that already qualify are used as-is."""
    if 5 <= len(idempotency_key) <= 1024 and all(c in "abcdefghijklmnopqrstuv0123456789" for c in idempotency_key):
        return idempotency_key
    return "nc" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:40]


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code in (401, 403):
        raise CalendarAuthError(f"http_{resp.status_code}")
    if resp.status_code == 429 or resp.status_code >= 500:
        raise CalendarUnavailable(f"http_{resp.status_code}")
    if resp.status_code >= 400:
        raise CalendarRejected(f"http_{resp.status_code}")


class GoogleCalendarProvider:
    name = PROVIDER

    def __init__(
        self, api_base: str, *, timeout: float = 8.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base = api_base.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    async def _request(self, method: str, path: str, token: str, **kw: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport, follow_redirects=False
            ) as client:
                return await client.request(
                    method, f"{self._base}{path}", headers={"Authorization": f"Bearer {token}"}, **kw
                )
        except httpx.HTTPError as exc:
            raise CalendarUnavailable(type(exc).__name__) from exc

    async def busy(self, access_token: str, calendar_id: str, start: datetime, end: datetime) -> list[BusyInterval]:
        resp = await self._request(
            "POST",
            "/freeBusy",
            access_token,
            json={"timeMin": _rfc3339(start), "timeMax": _rfc3339(end), "items": [{"id": calendar_id}]},
        )
        _raise_for(resp)
        cal = (resp.json().get("calendars") or {}).get(calendar_id) or {}
        if cal.get("errors"):
            raise CalendarRejected("freebusy_calendar_error")
        return [BusyInterval(start=b["start"], end=b["end"]) for b in cal.get("busy", [])]

    async def create_event(self, access_token: str, calendar_id: str, event: CalendarEvent) -> str:
        event_id = event_id_from_key(event.idempotency_key)
        body: dict[str, Any] = {
            "id": event_id,
            "summary": event.summary,
            "start": {"dateTime": _rfc3339(event.start)},
            "end": {"dateTime": _rfc3339(event.end)},
        }
        if event.attendee_email:
            body["attendees"] = [{"email": event.attendee_email}]
        resp = await self._request(
            "POST", f"/calendars/{_q(calendar_id)}/events", access_token, params={"sendUpdates": "none"}, json=body
        )
        if resp.status_code == 409:  # the deterministic id already exists: an earlier attempt succeeded
            return event_id
        _raise_for(resp)
        return str(resp.json().get("id") or event_id)

    async def cancel_event(self, access_token: str, calendar_id: str, event_id: str) -> None:
        resp = await self._request(
            "DELETE",
            f"/calendars/{_q(calendar_id)}/events/{_q(event_id)}",
            access_token,
            params={"sendUpdates": "none"},
        )
        if resp.status_code in (404, 410):  # already gone: deleting is idempotent
            return
        _raise_for(resp)

    async def event_exists(self, access_token: str, calendar_id: str, event_id: str) -> bool:
        """Reconciliation probe after an ambiguous write."""
        resp = await self._request("GET", f"/calendars/{_q(calendar_id)}/events/{_q(event_id)}", access_token)
        if resp.status_code in (404, 410):
            return False
        _raise_for(resp)
        return resp.json().get("status") != "cancelled"


@dataclass
class GoogleTokenSource:
    """Exchanges the tenant's refresh token (read from Vault via `CredentialResolver`) for a short-lived access
    token. Access tokens live in process memory only, never in Redis, the database, logs or events."""

    resolver: CredentialResolver
    token_url: str
    timeout: float = 8.0
    transport: httpx.AsyncBaseTransport | None = None
    _cache: dict[str, tuple[float, str]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def invalidate(self, tenant_id: object) -> None:
        self._cache.pop(str(tenant_id), None)
        self.resolver.invalidate(str(tenant_id), PROVIDER)

    async def access_token(self, tenant_id: object) -> str:
        key = str(tenant_id)
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        async with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            creds = await self.resolver.resolve(key, PROVIDER)
            token, ttl = await self._refresh(creds)
            self._cache[key] = (time.monotonic() + max(ttl - 60, 30), token)
            return token

    async def _refresh(self, creds: ResolvedCredentials) -> tuple[str, float]:
        if creds.source == "fake":
            secret = creds.secret("fake_secret")
            form = {"client_id": "nexus-fake-client", "client_secret": secret, "refresh_token": secret}
        else:
            form = {
                "client_id": creds.secret("client_id"),
                "client_secret": creds.secret("client_secret"),
                "refresh_token": creds.secret("refresh_token"),
            }
        form["grant_type"] = "refresh_token"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self.transport, follow_redirects=False
            ) as client:
                resp = await client.post(self.token_url, data=form)
        except httpx.HTTPError as exc:
            raise CalendarUnavailable(type(exc).__name__) from exc
        finally:
            form.clear()
        if resp.status_code in (400, 401):  # invalid_grant / invalid_client: the connection needs attention
            raise CalendarAuthError(f"token_http_{resp.status_code}")
        _raise_for(resp)
        data = resp.json()
        return str(data["access_token"]), float(data.get("expires_in", 3600))


def build_calendar(settings: ServiceSettings, transport: httpx.AsyncBaseTransport | None = None) -> Any:
    s: Any = settings
    fake_base = f"{settings.fake_providers_url.rstrip('/')}/google_calendar/calendar/v3"
    return select_adapter(
        settings,
        real=lambda: GoogleCalendarProvider(s.google_calendar_api_base, timeout=s.calendar_timeout_seconds,
                                            transport=transport),
        fake=lambda: GoogleCalendarProvider(fake_base, timeout=s.calendar_timeout_seconds, transport=transport),
        name=PROVIDER,
    )  # fmt: skip


def token_url_for(settings: ServiceSettings) -> str:
    if settings.provider_mode is ProviderMode.FAKE:
        return f"{settings.fake_providers_url.rstrip('/')}/google_calendar/token"
    return str(getattr(settings, "google_token_url", "https://oauth2.googleapis.com/token"))

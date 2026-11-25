"""Twilio Programmable Voice REST adapter (https://www.twilio.com/docs/voice/api/call-resource).

Error semantics matter more than anything else here (build.md §3.1G):
* the request never left (DNS/connect failure)      -> `NotSent`: safe to try again;
* a timeout or 5xx after the request was sent       -> `AmbiguousProviderResult`: the call may exist; reconcile;
* a 4xx                                              -> `ProviderRejected`: permanent, never retried (429 excepted).
Every request has a timeout and never follows redirects. Credentials stay in memory for the request only.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from nexus_providers.telephony import (
    AmbiguousProviderResult,
    CreateCallRequest,
    ProviderCall,
    ProviderRejected,
    TelephonyCredentials,
)

_SID = re.compile(r"^CA[0-9a-fA-F]{32}$")
_ACCOUNT = re.compile(r"^AC[0-9a-fA-F]{32}$")
_STATUSES = {"queued", "initiated", "ringing", "in-progress", "completed", "busy", "failed", "no-answer", "canceled"}


class NotSent(Exception):
    """The request did not reach the provider (connection/DNS error). Nothing was created."""


class ProviderThrottled(Exception):
    """429: the provider refused before acting. Retry later."""


class ProviderCallNotFound(Exception):
    pass


def _price_micros(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return round(abs(float(value)) * 1_000_000)
    except (TypeError, ValueError):
        return None


def to_provider_call(data: dict[str, Any]) -> ProviderCall:
    status = str(data.get("status") or "unknown")
    duration = data.get("duration")
    return ProviderCall(
        provider_call_id=str(data["sid"]),
        status=status if status in _STATUSES else "unknown",  # type: ignore[arg-type]
        duration_seconds=int(duration) if duration not in (None, "") else None,
        price_micros=_price_micros(data.get("price")),
    )


class TwilioRest:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        token_lookup: bool = False,
        name: str = "twilio",
    ) -> None:
        self.name = name
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport
        # Only the simulator can search by idempotency token; real Twilio is searched by To/From/date.
        self._token_lookup = token_lookup

    def _account_url(self, creds: TelephonyCredentials, suffix: str) -> str:
        if not _ACCOUNT.match(creds.account_sid):
            raise ProviderRejected("invalid_account_sid")
        return f"{self._base}/2010-04-01/Accounts/{creds.account_sid}/{suffix}"

    async def _send(
        self,
        method: str,
        url: str,
        creds: TelephonyCredentials,
        *,
        data: list[tuple[str, str]] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        all_headers = dict(headers or {})
        content = None
        if data is not None:  # repeated keys (StatusCallbackEvent) need explicit form encoding
            content = urlencode(data).encode()
            all_headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport, follow_redirects=False
            ) as client:
                return await client.request(
                    method,
                    url,
                    content=content,
                    params=params,
                    headers=all_headers,
                    auth=(creds.account_sid, creds.auth_token),
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise NotSent(type(exc).__name__) from exc
        except httpx.TimeoutException as exc:  # read/write/pool timeout after sending: outcome unknown
            raise AmbiguousProviderResult(type(exc).__name__) from exc
        except httpx.HTTPError as exc:  # protocol errors mid-response: outcome unknown
            raise AmbiguousProviderResult(type(exc).__name__) from exc

    @staticmethod
    def _error_code(resp: httpx.Response) -> str:
        try:
            code = resp.json().get("code")
        except ValueError:
            code = None
        return f"twilio_{code}" if code else f"http_{resp.status_code}"

    async def create_call(self, creds: TelephonyCredentials, req: CreateCallRequest) -> ProviderCall:
        form: list[tuple[str, str]] = [
            ("To", req.to),
            ("From", req.from_),
            ("Url", req.answer_url),
            ("Method", "POST"),
            ("StatusCallback", req.status_callback_url),
            ("StatusCallbackMethod", "POST"),
            ("Timeout", str(req.timeout_seconds)),
        ]
        form += [("StatusCallbackEvent", e) for e in ("initiated", "ringing", "answered", "completed")]
        if req.machine_detection:
            form.append(("MachineDetection", "Enable"))
        resp = await self._send(
            "POST",
            self._account_url(creds, "Calls.json"),
            creds,
            data=form,
            headers={"I-Twilio-Idempotency-Token": req.idempotency_key},
        )
        if resp.status_code in (200, 201):
            return to_provider_call(resp.json())
        if resp.status_code == 429:
            raise ProviderThrottled(self._error_code(resp))
        if resp.status_code >= 500:
            raise AmbiguousProviderResult(f"http_{resp.status_code}")
        raise ProviderRejected(self._error_code(resp))

    async def get_call(self, creds: TelephonyCredentials, provider_call_id: str) -> ProviderCall:
        if not _SID.match(provider_call_id):
            raise ProviderCallNotFound(provider_call_id)
        resp = await self._send("GET", self._account_url(creds, f"Calls/{provider_call_id}.json"), creds)
        if resp.status_code == 404:
            raise ProviderCallNotFound(provider_call_id)
        if resp.status_code != 200:
            raise AmbiguousProviderResult(f"http_{resp.status_code}")
        return to_provider_call(resp.json())

    async def find_call_by_idempotency_key(self, creds: TelephonyCredentials, key: str) -> ProviderCall | None:
        if not self._token_lookup:
            # Real Twilio has no lookup by idempotency token; callers must use `find_call` with To/From/date.
            raise AmbiguousProviderResult("lookup_by_token_unsupported")
        resp = await self._send("GET", self._account_url(creds, "Calls.json"), creds, params={"IdempotencyToken": key})
        if resp.status_code != 200:
            raise AmbiguousProviderResult(f"http_{resp.status_code}")
        calls = resp.json().get("calls") or []
        return to_provider_call(calls[0]) if calls else None

    async def find_call(
        self, creds: TelephonyCredentials, *, idempotency_key: str, to: str, from_: str, created_after: datetime
    ) -> ProviderCall | None:
        """Reconciliation lookup after an ambiguous create. Returns None only when the provider confirms that no
        matching call exists; any doubt raises `AmbiguousProviderResult` so the caller does NOT retry."""
        if self._token_lookup:
            return await self.find_call_by_idempotency_key(creds, idempotency_key)
        resp = await self._send(
            "GET", self._account_url(creds, "Calls.json"), creds, params={"To": to, "From": from_, "PageSize": "50"}
        )
        if resp.status_code != 200:
            raise AmbiguousProviderResult(f"http_{resp.status_code}")
        window_start = created_after - timedelta(seconds=60)
        matches: list[dict[str, Any]] = []
        for item in resp.json().get("calls") or []:
            try:
                created = datetime.strptime(str(item.get("date_created")), "%a, %d %b %Y %H:%M:%S %z")
            except ValueError:
                continue
            if created >= window_start and item.get("direction", "outbound-api").startswith("outbound"):
                matches.append(item)
        if len(matches) > 1:
            raise AmbiguousProviderResult("multiple_candidate_calls")
        return to_provider_call(matches[0]) if matches else None

    async def hangup(self, creds: TelephonyCredentials, provider_call_id: str, *, status: str = "completed") -> None:
        if status not in ("completed", "canceled") or not _SID.match(provider_call_id):
            raise ProviderRejected("invalid_hangup")
        resp = await self._send(
            "POST", self._account_url(creds, f"Calls/{provider_call_id}.json"), creds, data=[("Status", status)]
        )
        if resp.status_code in (200, 201):
            return
        if resp.status_code >= 500:
            raise AmbiguousProviderResult(f"http_{resp.status_code}")
        raise ProviderRejected(self._error_code(resp))

    async def update_twiml(self, creds: TelephonyCredentials, provider_call_id: str, twiml: str) -> None:
        if not _SID.match(provider_call_id):
            raise ProviderRejected("invalid_call_sid")
        resp = await self._send(
            "POST", self._account_url(creds, f"Calls/{provider_call_id}.json"), creds, data=[("Twiml", twiml)]
        )
        if resp.status_code in (200, 201):
            return
        if resp.status_code >= 500:
            raise AmbiguousProviderResult(f"http_{resp.status_code}")
        raise ProviderRejected(self._error_code(resp))

    async def transfer_to_conference(
        self, creds: TelephonyCredentials, provider_call_id: str, conference_name: str
    ) -> None:
        from call_service.twiml import conference

        await self.update_twiml(
            creds, provider_call_id, conference(conference_name, "Please hold while I connect you to a team member.")
        )

    async def dial_into_conference(
        self, creds: TelephonyCredentials, *, to: str, from_: str, conference_name: str, idempotency_key: str
    ) -> ProviderCall:
        """Second leg: rings the human agent line and joins it to the handoff conference."""
        from xml.sax.saxutils import escape

        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?><Response><Dial><Conference startConferenceOnEnter="true">'
            f"{escape(conference_name)}</Conference></Dial></Response>"
        )
        resp = await self._send(
            "POST",
            self._account_url(creds, "Calls.json"),
            creds,
            data=[("To", to), ("From", from_), ("Twiml", twiml), ("Timeout", "20")],
            headers={"I-Twilio-Idempotency-Token": idempotency_key},
        )
        if resp.status_code in (200, 201):
            return to_provider_call(resp.json())
        if resp.status_code >= 500:
            raise AmbiguousProviderResult(f"http_{resp.status_code}")
        raise ProviderRejected(self._error_code(resp))

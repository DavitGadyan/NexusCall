"""DEVELOPMENT/TEST ONLY Google Calendar + Google OAuth simulator (in-memory, deterministic).

Mirrors the subset of the documented APIs NexusCall uses, under `/google_calendar`:

* OAuth 2.0: `GET /o/oauth2/v2/auth` (consent is auto-granted: 302 back to `redirect_uri` with `code` and
  `state`), `POST /token` (`authorization_code` with PKCE S256 check, and `refresh_token`).
* Calendar v3: `POST /calendar/v3/freeBusy`, `GET /calendar/v3/calendars/{id}`,
  `POST /calendar/v3/calendars/{id}/events` (409 when the client-chosen id exists),
  `GET|DELETE /calendar/v3/calendars/{id}/events/{eventId}`.
* Test hooks: `POST /_sim/busy` adds a busy block, `POST /_sim/fail` makes the next N calendar calls fail.

The development refresh token is `FAKE_PROVIDER_SECRET` (what services resolve in fake mode). Nothing here is
ever reachable outside development/test (see `fake_providers.main`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

router = APIRouter(prefix="/google_calendar", tags=["fake-google-calendar"])

ACCOUNT_EMAIL = "demo.calendar@example.test"
SCOPES = "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.readonly"


class _State:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.codes: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: set[str] = set()
        self.access_tokens: set[str] = set()
        self.events: dict[tuple[str, str], dict[str, Any]] = {}
        self.busy: dict[str, list[tuple[str, str]]] = {}
        self.fail_next = 0
        self.fail_status = 503
        self.calls: list[tuple[str, str]] = []


STATE = _State()


async def _form(request: Request) -> dict[str, str]:
    """application/x-www-form-urlencoded body (python-multipart is not a dependency of the simulators)."""
    parsed = parse_qs((await request.body()).decode(), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def _secret() -> str:
    return os.environ.get("FAKE_PROVIDER_SECRET") or "fake-provider-secret-dev-only"


def _error(status: int, reason: str) -> JSONResponse:
    return JSONResponse({"error": {"code": status, "message": reason, "status": reason}}, status_code=status)


def _authorized(authorization: str | None) -> bool:
    token = (authorization or "").removeprefix("Bearer ").strip()
    return token in STATE.access_tokens


def _guard(authorization: str | None, op: str) -> JSONResponse | None:
    STATE.calls.append((op, (authorization or "")[:7]))
    if STATE.fail_next > 0:
        STATE.fail_next -= 1
        return _error(STATE.fail_status, "backendError")
    if not _authorized(authorization):
        return _error(401, "UNAUTHENTICATED")
    return None


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ------------------------------------------------------------------------------------------------ OAuth
@router.get("/o/oauth2/v2/auth")
async def authorize(
    client_id: str,
    redirect_uri: str,
    state: str,
    response_type: str = "code",
    scope: str = SCOPES,
    code_challenge: str | None = None,
    code_challenge_method: str | None = None,
) -> Response:
    parts = urlsplit(redirect_uri)
    if response_type != "code" or parts.scheme not in ("http", "https") or not parts.netloc:
        return _error(400, "invalid_request")
    code = "fake-gcal-code-" + secrets.token_urlsafe(16)
    STATE.codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "challenge": code_challenge,
        "method": code_challenge_method,
    }
    return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", status_code=302)


def _issue_access(refresh_token: str) -> str:
    token = "fake-gcal-at-" + hmac.new(_secret().encode(), refresh_token.encode(), hashlib.sha256).hexdigest()[:32]
    STATE.access_tokens.add(token)
    return token


@router.post("/token")
async def token(request: Request) -> JSONResponse:
    form = await _form(request)
    grant_type = form.get("grant_type", "")
    client_id = form.get("client_id", "")
    client_secret = form.get("client_secret", "")
    code = form.get("code", None)
    redirect_uri = form.get("redirect_uri", None)
    code_verifier = form.get("code_verifier", None)
    refresh_token = form.get("refresh_token", None)
    if not client_id or not client_secret:
        return JSONResponse({"error": "invalid_client"}, status_code=401)
    if grant_type == "authorization_code":
        grant = STATE.codes.pop(code or "", None)  # single use
        if grant is None or grant["redirect_uri"] != redirect_uri or grant["client_id"] != client_id:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if grant["challenge"]:
            digest = hashlib.sha256((code_verifier or "").encode()).digest()
            expected = base64.urlsafe_b64encode(digest).decode().rstrip("=")
            if grant["method"] != "S256" or not hmac.compare_digest(expected, grant["challenge"]):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
        rt = "fake-gcal-rt-" + secrets.token_urlsafe(24)
        STATE.refresh_tokens.add(rt)
        return JSONResponse(
            {
                "access_token": _issue_access(rt),
                "refresh_token": rt,
                "expires_in": 3599,
                "token_type": "Bearer",
                "scope": grant["scope"],
            }
        )
    if grant_type == "refresh_token":
        if refresh_token != _secret() and refresh_token not in STATE.refresh_tokens:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return JSONResponse(
            {"access_token": _issue_access(refresh_token or ""), "expires_in": 3599, "token_type": "Bearer",
             "scope": SCOPES}
        )  # fmt: skip
    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


# ------------------------------------------------------------------------------------------------ calendar
@router.get("/calendar/v3/calendars/{calendar_id}")
async def get_calendar(calendar_id: str, authorization: str | None = Header(default=None)) -> Response:
    if (err := _guard(authorization, "calendars.get")) is not None:
        return err
    cid = ACCOUNT_EMAIL if calendar_id == "primary" else calendar_id
    return JSONResponse({"kind": "calendar#calendar", "id": cid, "summary": cid, "timeZone": "UTC"})


@router.post("/calendar/v3/freeBusy")
async def free_busy(request: Request, authorization: str | None = Header(default=None)) -> Response:
    if (err := _guard(authorization, "freebusy")) is not None:
        return err
    body = await request.json()
    lo, hi = _parse(body["timeMin"]), _parse(body["timeMax"])
    calendars: dict[str, Any] = {}
    for item in body.get("items", []):
        cid = item["id"]
        blocks = list(STATE.busy.get(cid, []))
        blocks += [(e["start"]["dateTime"], e["end"]["dateTime"]) for (c, _), e in STATE.events.items()
                   if c == cid and e["status"] != "cancelled"]  # fmt: skip
        overlapping = [{"start": s, "end": e} for s, e in sorted(blocks) if _parse(s) < hi and _parse(e) > lo]
        calendars[cid] = {"busy": overlapping}
    return JSONResponse({"kind": "calendar#freeBusy", "timeMin": body["timeMin"], "timeMax": body["timeMax"],
                         "calendars": calendars})  # fmt: skip


@router.post("/calendar/v3/calendars/{calendar_id}/events")
async def insert_event(
    calendar_id: str, request: Request, authorization: str | None = Header(default=None)
) -> Response:
    if (err := _guard(authorization, "events.insert")) is not None:
        return err
    body = await request.json()
    event_id = body.get("id") or secrets.token_hex(16)
    if (calendar_id, event_id) in STATE.events:
        return _error(409, "duplicate")
    event = {**body, "id": event_id, "status": "confirmed", "kind": "calendar#event"}
    STATE.events[(calendar_id, event_id)] = event
    return JSONResponse(event)


@router.get("/calendar/v3/calendars/{calendar_id}/events/{event_id}")
async def get_event(calendar_id: str, event_id: str, authorization: str | None = Header(default=None)) -> Response:
    if (err := _guard(authorization, "events.get")) is not None:
        return err
    event = STATE.events.get((calendar_id, event_id))
    if event is None:
        return _error(404, "notFound")
    return JSONResponse(event)


@router.delete("/calendar/v3/calendars/{calendar_id}/events/{event_id}")
async def delete_event(calendar_id: str, event_id: str, authorization: str | None = Header(default=None)) -> Response:
    if (err := _guard(authorization, "events.delete")) is not None:
        return err
    event = STATE.events.get((calendar_id, event_id))
    if event is None or event["status"] == "cancelled":
        return _error(410, "deleted")
    event["status"] = "cancelled"
    return Response(status_code=204)


# ------------------------------------------------------------------------------------------------ test hooks
@router.post("/_sim/busy", include_in_schema=False)
async def add_busy(request: Request) -> dict[str, str]:
    body = await request.json()
    STATE.busy.setdefault(body["calendar_id"], []).append((body["start"], body["end"]))
    return {"status": "ok"}


@router.post("/_sim/fail", include_in_schema=False)
async def fail_next(request: Request) -> dict[str, str]:
    body = await request.json()
    STATE.fail_next = int(body.get("count", 1))
    STATE.fail_status = int(body.get("status", 503))
    return {"status": "ok"}


@router.post("/_sim/reset", include_in_schema=False)
async def reset() -> dict[str, str]:
    STATE.reset()
    return {"status": "ok"}

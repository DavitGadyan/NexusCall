"""DEVELOPMENT/TEST ONLY HubSpot simulator (in-memory, deterministic) under `/hubspot`.

Documented endpoints mirrored:
* OAuth: `GET /oauth/authorize` (auto-consent, 302 to `redirect_uri` with `code` + `state`),
  `POST /oauth/v1/token` (`authorization_code`, `refresh_token`),
  `GET /oauth/v1/access-tokens/{token}` (token metadata: hub_id, user, scopes).
* `GET /account-info/v3/details` (connection test for private-app and OAuth tokens).

Accepted bearer tokens: ones issued here, `FAKE_PROVIDER_SECRET`, and private-app style `pat-fake-*` tokens.
"""

from __future__ import annotations

import os
import secrets
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

router = APIRouter(prefix="/hubspot", tags=["fake-hubspot"])

HUB_ID = 990001
SCOPES = ["crm.objects.contacts.read", "crm.objects.contacts.write", "oauth"]


class _State:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.codes: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: set[str] = set()
        self.access_tokens: set[str] = set()


STATE = _State()


async def _form(request: Request) -> dict[str, str]:
    """application/x-www-form-urlencoded body (python-multipart is not a dependency of the simulators)."""
    parsed = parse_qs((await request.body()).decode(), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def _secret() -> str:
    return os.environ.get("FAKE_PROVIDER_SECRET") or "fake-provider-secret-dev-only"


def _valid(token: str) -> bool:
    return token in STATE.access_tokens or token == _secret() or token.startswith("pat-fake-")


def _err(status: int, category: str) -> JSONResponse:
    return JSONResponse({"status": "error", "message": category, "category": category}, status_code=status)


@router.get("/oauth/authorize")
async def authorize(client_id: str, redirect_uri: str, state: str, scope: str = " ".join(SCOPES)) -> Response:
    parts = urlsplit(redirect_uri)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return _err(400, "BAD_REDIRECT_URI")
    code = "fake-hs-code-" + secrets.token_urlsafe(16)
    STATE.codes[code] = {"client_id": client_id, "redirect_uri": redirect_uri, "scope": scope}
    return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", status_code=302)


def _tokens() -> dict[str, Any]:
    access = "fake-hs-at-" + secrets.token_urlsafe(24)
    refresh = "fake-hs-rt-" + secrets.token_urlsafe(24)
    STATE.access_tokens.add(access)
    STATE.refresh_tokens.add(refresh)
    return {"access_token": access, "refresh_token": refresh, "expires_in": 1800, "token_type": "bearer"}


@router.post("/oauth/v1/token")
async def token(request: Request) -> Response:
    form = await _form(request)
    grant_type = form.get("grant_type", "")
    client_id = form.get("client_id", "")
    client_secret = form.get("client_secret", "")
    code = form.get("code", None)
    redirect_uri = form.get("redirect_uri", None)
    refresh_token = form.get("refresh_token", None)
    if not client_id or not client_secret:
        return _err(400, "BAD_CLIENT_ID")
    if grant_type == "authorization_code":
        grant = STATE.codes.pop(code or "", None)
        if grant is None or grant["redirect_uri"] != redirect_uri or grant["client_id"] != client_id:
            return _err(400, "BAD_AUTH_CODE")
        return JSONResponse(_tokens())
    if grant_type == "refresh_token":
        if refresh_token not in STATE.refresh_tokens and refresh_token != _secret():
            return _err(400, "BAD_REFRESH_TOKEN")
        return JSONResponse(_tokens())
    return _err(400, "BAD_GRANT_TYPE")


@router.get("/oauth/v1/access-tokens/{token}")
async def token_info(token: str) -> Response:
    if not _valid(token):
        return _err(404, "NOT_FOUND")
    return JSONResponse(
        {"token": "[redacted]", "user": "owner@example.test", "hub_domain": "demo.example.test", "scopes": SCOPES,
         "hub_id": HUB_ID, "app_id": 123, "expires_in": 1800, "user_id": 42, "token_type": "access"}
    )  # fmt: skip


@router.get("/account-info/v3/details")
async def account_details(authorization: str | None = Header(default=None)) -> Response:
    if not _valid((authorization or "").removeprefix("Bearer ").strip()):
        return _err(401, "INVALID_AUTHENTICATION")
    return JSONResponse({"portalId": HUB_ID, "accountType": "DEVELOPER_TEST", "timeZone": "UTC",
                         "companyCurrency": "USD", "uiDomain": "app.hubspot.com"})  # fmt: skip

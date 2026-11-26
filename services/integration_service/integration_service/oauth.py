"""OAuth 2.0 authorization-code connections for Google Calendar and HubSpot (build.md §3.1M, §19.3).

* `start`: a 256-bit random `state` is bound server-side to (tenant, user, provider, redirect URI) with a
  10-minute expiry. Only its SHA-256 is stored. A second random value is set as an HttpOnly, SameSite=Lax
  cookie scoped to the callback path; the callback must present it (login-CSRF defence: a victim cannot be
  made to complete an attacker's flow). PKCE (S256) is used where the provider supports it (Google); the
  verifier is HMAC(server key, state), so it is never stored anywhere.
* `callback`: the state row is consumed atomically (single use), then the code is exchanged, the refresh
  token goes straight to Vault through the normal connection lifecycle, and the browser is redirected to a
  fixed UI path (no open redirect). Token values never reach the browser, logs or the database.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from integration_service.connections import Scope, create_connection, rotate_connection
from integration_service.deps import db, settings_of
from integration_service.providers.catalogue import OAUTH_CLIENT
from integration_service.providers.testers import TestResult, google_urls, hubspot_api
from integration_service.secrets import SecretsUnavailable
from integration_service.tables import oauth_states, provider_connections
from nexus_core.config import ProviderMode
from nexus_core.context import Actor
from nexus_core.errors import Conflict, ProblemError, Unprocessable
from nexus_core.vault import platform_secret_path

log = logging.getLogger(__name__)
BIND_COOKIE = "nx_oauth_bind"


class OAuthRejected(ProblemError):
    status, code, title = 400, "oauth_rejected", "Authorization could not be completed"


@dataclass(frozen=True, slots=True)
class OAuthProvider:
    key: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    pkce: bool
    extra: tuple[tuple[str, str], ...] = ()


def providers(app: FastAPI) -> dict[str, OAuthProvider]:
    s = settings_of(app)
    g_token, _ = google_urls(s)
    hs_api = hubspot_api(s)
    if s.provider_mode is ProviderMode.FAKE:
        public = (s.fake_providers_public_url or s.fake_providers_url).rstrip("/")
        g_auth, hs_auth = f"{public}/google_calendar/o/oauth2/v2/auth", f"{public}/hubspot/oauth/authorize"
    else:
        g_auth, hs_auth = "https://accounts.google.com/o/oauth2/v2/auth", "https://app.hubspot.com/oauth/authorize"
    return {
        "google_calendar": OAuthProvider(
            "google_calendar", g_auth, g_token,
            ("https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/calendar.readonly"),
            pkce=True, extra=(("access_type", "offline"), ("prompt", "consent"), ("include_granted_scopes", "true")),
        ),
        # HubSpot's public OAuth does not document PKCE; state + client secret + binding cookie apply.
        "hubspot": OAuthProvider(
            "hubspot", hs_auth, f"{hs_api}/oauth/v1/token",
            ("crm.objects.contacts.read", "crm.objects.contacts.write", "oauth"), pkce=False,
        ),
    }  # fmt: skip


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def pkce_verifier(app: FastAPI, state: str) -> str:
    return _b64(hmac.new(app.state.hmac_key, f"pkce:{state}".encode(), hashlib.sha256).digest())


def redirect_uri(app: FastAPI) -> str:
    s = settings_of(app)
    return f"{s.public_base_url.rstrip('/')}{s.oauth_redirect_path}"


async def client_credentials(app: FastAPI, provider: str) -> dict[str, SecretStr]:
    """The platform's OAuth app registration, read from Vault (fake mode: a development-only client)."""
    s = settings_of(app)
    if s.provider_mode is ProviderMode.FAKE:
        secret = s.fake_provider_secret or SecretStr("fake-provider-secret-dev-only")
        return {"client_id": SecretStr("nexus-fake-client"), "client_secret": secret}
    path = platform_secret_path(s.env.value, f"integration-service/{OAUTH_CLIENT[provider]}")
    try:
        return await app.state.secrets.read(path)
    except SecretsUnavailable as exc:
        raise Conflict("The platform OAuth app for this provider is not configured") from exc


# ------------------------------------------------------------------------------------------- start
async def start(app: FastAPI, tenant_id: uuid.UUID, actor: Actor, provider: str) -> tuple[str, str, datetime]:
    """Returns (authorization_url, binding_cookie_value, expires_at)."""
    spec = providers(app).get(provider)
    if spec is None:
        raise Unprocessable("This provider does not support OAuth connections")
    client = await client_credentials(app, provider)
    state, binding = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    ttl = settings_of(app).oauth_state_ttl_seconds
    expires = datetime.now(UTC) + timedelta(seconds=ttl)
    uri = redirect_uri(app)
    async with db(app).global_session(actor) as s:
        await s.execute(
            insert(oauth_states).values(
                state_hash=_sha(state),
                binding_hash=_sha(binding),
                tenant_id=tenant_id,
                user_id=actor.id,
                provider=provider,
                redirect_uri=uri,
                expires_at=expires,
            )
        )
    params = [
        ("client_id", client["client_id"].get_secret_value()),
        ("redirect_uri", uri),
        ("response_type", "code"),
        ("scope", " ".join(spec.scopes)),
        ("state", state),
        *spec.extra,
    ]
    if spec.pkce:
        params += [("code_challenge", _b64(hashlib.sha256(pkce_verifier(app, state).encode()).digest())),
                   ("code_challenge_method", "S256")]  # fmt: skip
    return f"{spec.authorize_url}?{urlencode(params)}", binding, expires


# ------------------------------------------------------------------------------------------- callback
async def _consume(app: FastAPI, state: str) -> dict[str, Any] | None:
    async with db(app).global_session() as s:
        row = (
            (
                await s.execute(
                    update(oauth_states)
                    .where(
                        oauth_states.c.state_hash == _sha(state),
                        oauth_states.c.used_at.is_(None),
                        oauth_states.c.expires_at > func.now(),
                    )
                    .values(used_at=func.now(), outcome="consumed")
                    .returning(oauth_states)
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def _mark(app: FastAPI, state_id: uuid.UUID, outcome: str) -> None:
    async with db(app).global_session() as s:
        await s.execute(update(oauth_states).where(oauth_states.c.id == state_id).values(outcome=outcome))


async def callback(
    app: FastAPI, *, state: str | None, code: str | None, error: str | None, binding: str | None
) -> dict[str, Any]:
    """Completes the flow. Raises `OAuthRejected` (uniform 400) for any state problem."""
    if not state or len(state) > 200:
        raise OAuthRejected("Missing or invalid state")
    row = await _consume(app, state)
    if row is None:  # unknown, expired or already used (replay)
        raise OAuthRejected("Unknown, expired or already used state")
    if not binding or not hmac.compare_digest(_sha(binding), row["binding_hash"]):
        await _mark(app, row["id"], "csrf_rejected")
        log.warning("oauth_binding_mismatch", extra={"provider": row["provider"]})
        raise OAuthRejected("This authorization was not started from this browser")
    if error or not code:
        await _mark(app, row["id"], "denied")
        return {"status": "denied", "provider": row["provider"], "connection_id": None}
    spec = providers(app)[row["provider"]]
    client = await client_credentials(app, row["provider"])
    tokens = await _exchange(app, spec, client, code, row["redirect_uri"], state)
    tenant_id: uuid.UUID = row["tenant_id"]
    actor = Actor("user", row["user_id"])
    identity = await _identity(app, spec, tokens)
    values = _vault_values(row["provider"], client, tokens)
    scope = Scope(tenant_id)
    result = TestResult(True, account_id=identity.get("account"), scopes=identity.get("scopes") or [])
    async with db(app).tenant_session(tenant_id, actor) as s:
        existing = (
            await s.execute(
                select(provider_connections.c.id).where(
                    provider_connections.c.provider == row["provider"],
                    provider_connections.c.scope == "default",
                    provider_connections.c.status != "revoked",
                )
            )
        ).scalar()
    if existing is not None:  # reconnect: new credential replaces the old one through validated rotation
        conn = await rotate_connection(app, scope, actor, existing, values, reason="oauth_reconnect",
                                       test_override=result)  # fmt: skip
    else:
        conn = await create_connection(app, scope, actor, provider=row["provider"], credentials=values, config={},
                                       display_name=None, auth_type="oauth", test_override=result)  # fmt: skip
    if conn["auth_type"] != "oauth":
        async with db(app).tenant_session(tenant_id, actor) as s:
            await s.execute(
                update(provider_connections).where(provider_connections.c.id == conn["id"]).values(auth_type="oauth")
            )
    values.clear()
    tokens.clear()
    await _mark(app, row["id"], "connected")
    return {"status": conn["status"], "provider": row["provider"], "connection_id": str(conn["id"])}


async def _post_form(app: FastAPI, url: str, form: dict[str, str]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=app.state.egress_transport,
                                     follow_redirects=False) as c:  # fmt: skip
            resp = await c.post(url, data=form, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise OAuthRejected("The provider could not be reached") from exc
    finally:
        form.clear()
    if resp.status_code != 200:
        raise OAuthRejected(f"Token exchange failed (http {resp.status_code})")
    return dict(resp.json())


async def _exchange(app: FastAPI, spec: OAuthProvider, client: dict[str, SecretStr], code: str, uri: str,
                    state: str) -> dict[str, Any]:  # fmt: skip
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": uri,
        "client_id": client["client_id"].get_secret_value(),
        "client_secret": client["client_secret"].get_secret_value(),
    }
    if spec.pkce:
        form["code_verifier"] = pkce_verifier(app, state)
    tokens = await _post_form(app, spec.token_url, form)
    if spec.key == "google_calendar" and not tokens.get("refresh_token"):
        raise OAuthRejected("The provider did not grant offline access")
    if not tokens.get("access_token"):
        raise OAuthRejected("The provider returned no access token")
    return tokens


async def _identity(app: FastAPI, spec: OAuthProvider, tokens: dict[str, Any]) -> dict[str, Any]:
    """Connected account and granted scopes, shown to the admin (never the token)."""
    s = settings_of(app)
    access = str(tokens["access_token"])
    async with httpx.AsyncClient(timeout=10.0, transport=app.state.egress_transport, follow_redirects=False) as c:
        try:
            if spec.key == "google_calendar":
                _, api = google_urls(s)
                resp = await c.get(f"{api}/calendars/primary", headers={"Authorization": f"Bearer {access}"})
                account = resp.json().get("id") if resp.status_code == 200 else None
                return {"account": account, "scopes": str(tokens.get("scope", "")).split()}
            resp = await c.get(f"{hubspot_api(s)}/oauth/v1/access-tokens/{access}")
        except httpx.HTTPError as exc:
            raise OAuthRejected("The provider could not be reached") from exc
    if resp.status_code != 200:
        raise OAuthRejected("The provider did not confirm the connected account")
    info = resp.json()
    return {"account": str(info.get("hub_id")) if info.get("hub_id") else None, "scopes": list(info.get("scopes", []))}


def _vault_values(provider: str, client: dict[str, SecretStr], tokens: dict[str, Any]) -> dict[str, SecretStr]:
    if provider == "google_calendar":  # BUILD_CONVENTIONS §5: client_id, client_secret, refresh_token
        return {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "refresh_token": SecretStr(str(tokens["refresh_token"])),
        }
    values = {"access_token": SecretStr(str(tokens["access_token"]))}
    if tokens.get("refresh_token"):
        values |= {"refresh_token": SecretStr(str(tokens["refresh_token"])), "client_id": client["client_id"],
                   "client_secret": client["client_secret"]}  # fmt: skip
    return values


async def purge_states(app: FastAPI) -> int:
    from sqlalchemy import delete

    async with db(app).global_session() as s:
        result = await s.execute(delete(oauth_states).where(oauth_states.c.expires_at < func.now() - timedelta(days=1)))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]

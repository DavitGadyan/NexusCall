"""OAuth authorization-code connections: state is server-side, single use, expiring and bound to the browser
(CSRF); PKCE for Google; refresh tokens go to Vault only (build.md §19.3)."""

from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from nexus_core.testing import auth

from .conftest import MANAGE, PUBLIC

pytestmark = [pytest.mark.integration, pytest.mark.security]

GOOGLE_CLIENT = {"client_id": "1234-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-platform-app-secret"}
REFRESH = "1//0g-refresh-token-must-stay-in-vault"
ACCESS = "ya29.short-lived-access-token"


def _seed_google_client(h) -> None:
    h.vault.data["test/platform/integration-service/google_oauth_client"] = [dict(GOOGLE_CLIENT)]


def _start(h, tenant, provider="google_calendar", *, user=None):
    user = user or h.user(tenant, MANAGE)
    r = h.client.get(f"/api/v1/integrations/oauth/{provider}/start", headers=auth(user, tenant))
    assert r.status_code == 200, r.text
    query = parse_qs(urlsplit(r.json()["authorization_url"]).query)
    return r, {k: v[0] for k, v in query.items()}, r.cookies.get("nx_oauth_bind")


def _mock_google(h, *, verifier_sink: list[str]):
    def token(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        verifier_sink.append(form["code_verifier"][0])
        assert form["client_secret"] == [GOOGLE_CLIENT["client_secret"]]
        assert form["redirect_uri"] == [f"{PUBLIC}/oauth/callback"]
        return httpx.Response(200, json={"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3599,
                                         "scope": "https://www.googleapis.com/auth/calendar.events"})  # fmt: skip

    h.mock.post("https://oauth2.googleapis.com/token").mock(side_effect=token)
    h.mock.get("https://www.googleapis.com/calendar/v3/calendars/primary").mock(
        return_value=httpx.Response(200, json={"id": "dana.owner@example.com"})
    )


def _callback(h, params: dict[str, str], binding: str | None):
    h.client.cookies.clear()
    if binding:
        h.client.cookies.set("nx_oauth_bind", binding, path="/oauth/callback")
    try:
        return h.client.get("/oauth/callback", params=params, follow_redirects=False)
    finally:
        h.client.cookies.clear()


def test_google_flow_stores_refresh_token_in_vault_only(harness, tenant, caplog):
    caplog.set_level(logging.DEBUG)
    _seed_google_client(harness)
    sink: list[str] = []
    _mock_google(harness, verifier_sink=sink)
    start, query, binding = _start(harness, tenant)
    assert query["client_id"] == GOOGLE_CLIENT["client_id"] and query["access_type"] == "offline"
    assert query["code_challenge_method"] == "S256" and binding
    set_cookie = start.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=lax" in set_cookie and "path=/oauth/callback" in set_cookie
    assert GOOGLE_CLIENT["client_secret"] not in start.text
    # The state is stored only as a hash.
    rows = harness.sql("SELECT state_hash, tenant_id FROM oauth_states")
    assert hashlib.sha256(query["state"].encode()).hexdigest() in {r["state_hash"] for r in rows}
    assert query["state"] not in harness.dump_everything()

    r = _callback(harness, {"code": "auth-code-1", "state": query["state"]}, binding)
    assert r.status_code == 303, r.text
    target = urlsplit(r.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == f"{PUBLIC}/app/integrations"
    result = parse_qs(target.query)
    assert result["oauth"] == ["active"] and result["provider"] == ["google_calendar"]
    # PKCE: the verifier sent at exchange time matches the challenge sent to the browser.
    challenge = base64.urlsafe_b64encode(hashlib.sha256(sink[0].encode()).digest()).decode().rstrip("=")
    assert challenge == query["code_challenge"]
    connection_id = result["connection"][0]
    path = f"test/tenants/{tenant}/google_calendar/{connection_id}"
    assert harness.vault.latest(path) == {**GOOGLE_CLIENT, "refresh_token": REFRESH}
    detail = harness.client.get(f"/api/v1/integrations/connections/{connection_id}",
                                headers=auth(harness.user(tenant, MANAGE), tenant)).json()  # fmt: skip
    assert detail["auth_type"] == "oauth" and detail["status"] == "active"
    assert detail["account_masked"] == "d…@example.com"
    assert detail["granted_scopes"] == ["https://www.googleapis.com/auth/calendar.events"]
    everything = harness.dump_everything() + caplog.text + r.text + str(detail)
    for value in (REFRESH, ACCESS, GOOGLE_CLIENT["client_secret"], sink[0]):
        assert value not in everything


def test_state_cannot_be_reused(harness, tenant):
    _seed_google_client(harness)
    _mock_google(harness, verifier_sink=[])
    _, query, binding = _start(harness, tenant)
    params = {"code": "c", "state": query["state"]}
    assert _callback(harness, params, binding).status_code == 303
    replay = _callback(harness, params, binding)
    assert replay.status_code == 400 and replay.json()["code"] == "oauth_rejected"


def test_callback_without_the_browser_binding_is_rejected_as_csrf(harness, tenant):
    """Login-CSRF: an attacker's state + code delivered to a victim's browser lacks the binding cookie."""
    _seed_google_client(harness)
    _mock_google(harness, verifier_sink=[])
    _, query, binding = _start(harness, tenant)
    params = {"code": "attacker-code", "state": query["state"]}
    assert _callback(harness, params, None).status_code == 400
    # The state was consumed by the failed attempt; even the right browser cannot complete it now.
    assert _callback(harness, params, binding).status_code == 400
    outcome = harness.sql("SELECT outcome FROM oauth_states WHERE state_hash = :h",
                          {"h": hashlib.sha256(query["state"].encode()).hexdigest()})[0]["outcome"]  # fmt: skip
    assert outcome == "csrf_rejected"
    _, query2, _ = _start(harness, tenant)
    assert _callback(harness, {"code": "c", "state": query2["state"]}, "forged-binding").status_code == 400
    assert harness.sql("SELECT count(*) AS n FROM provider_connections", tenant_id=tenant)[0]["n"] == 0


def test_unknown_expired_and_missing_states_are_rejected_uniformly(harness, tenant):
    _seed_google_client(harness)
    _, query, binding = _start(harness, tenant)
    harness.sql("UPDATE oauth_states SET expires_at = now() - interval '1 second' WHERE state_hash = :h",
                {"h": hashlib.sha256(query["state"].encode()).hexdigest()})  # fmt: skip
    expired = _callback(harness, {"code": "c", "state": query["state"]}, binding)
    unknown = _callback(harness, {"code": "c", "state": "x" * 43}, binding)
    missing = _callback(harness, {"code": "c"}, binding)
    assert expired.status_code == unknown.status_code == missing.status_code == 400
    assert expired.json()["detail"] == unknown.json()["detail"]


def test_user_denial_redirects_without_creating_anything(harness, tenant):
    _seed_google_client(harness)
    _, query, binding = _start(harness, tenant)
    r = _callback(harness, {"error": "access_denied", "state": query["state"]}, binding)
    assert r.status_code == 303 and "oauth=denied" in r.headers["location"]
    assert harness.sql("SELECT count(*) AS n FROM provider_connections", tenant_id=tenant)[0]["n"] == 0


def test_oauth_start_requires_manage_permission_and_step_up(harness, tenant):
    _seed_google_client(harness)
    reader = harness.user(tenant, {"integrations:read"})
    no_mfa = harness.user(tenant, MANAGE, mfa=False)
    path = "/api/v1/integrations/oauth/google_calendar/start"
    assert harness.client.get(path, headers=auth(reader, tenant)).status_code == 403
    assert harness.client.get(path, headers=auth(no_mfa, tenant)).status_code == 401
    assert harness.client.get("/api/v1/integrations/oauth/twilio/start",
                              headers=auth(harness.user(tenant, MANAGE), tenant)).status_code == 422  # fmt: skip


def test_missing_platform_oauth_app_is_reported(harness, tenant):
    r = harness.client.get("/api/v1/integrations/oauth/google_calendar/start",
                           headers=auth(harness.user(tenant, MANAGE), tenant))  # fmt: skip
    assert r.status_code == 409


def test_hubspot_flow_against_the_simulator_maps_the_portal(fake_harness, tenant):
    h = fake_harness
    start, query, binding = _start(h, tenant, "hubspot")
    assert "code_challenge" not in query  # HubSpot: no documented PKCE support

    async def follow() -> httpx.Response:  # the "browser" visiting the provider's consent page
        async with httpx.AsyncClient(transport=h.sim_transport, follow_redirects=False) as c:
            return await c.get(start.json()["authorization_url"])

    consent = h.run(follow())
    assert consent.status_code == 302
    back = parse_qs(urlsplit(consent.headers["location"]).query)
    r = _callback(h, {"code": back["code"][0], "state": back["state"][0]}, binding)
    assert r.status_code == 303 and "oauth=active" in r.headers["location"], r.text
    connection_id = parse_qs(urlsplit(r.headers["location"]).query)["connection"][0]
    stored = h.vault.latest(f"test/tenants/{tenant}/hubspot/{connection_id}")
    assert set(stored) == {"access_token", "refresh_token", "client_id", "client_secret"}
    mapping = h.sql(
        "SELECT tenant_id, connection_id FROM inbound_webhook_registry WHERE external_account_id = '990001'"
    )
    assert str(mapping[0]["tenant_id"]) == str(tenant) and str(mapping[0]["connection_id"]) == connection_id
    # Reconnecting replaces the credential on the same connection (validated rotation, new Vault version).
    start2, _, binding2 = _start(h, tenant, "hubspot")

    async def follow2() -> httpx.Response:
        async with httpx.AsyncClient(transport=h.sim_transport, follow_redirects=False) as c:
            return await c.get(start2.json()["authorization_url"])

    back2 = parse_qs(urlsplit(h.run(follow2()).headers["location"]).query)
    r2 = _callback(h, {"code": back2["code"][0], "state": back2["state"][0]}, binding2)
    assert parse_qs(urlsplit(r2.headers["location"]).query)["connection"][0] == connection_id
    assert len(h.vault.data[f"test/tenants/{tenant}/hubspot/{connection_id}"]) == 2


def test_google_connection_test_against_the_simulator(fake_harness, tenant):
    """Connection tests run the real adapter code against the simulator in fake mode."""
    h = fake_harness
    user = h.user(tenant, MANAGE)
    body = {"provider": "google_calendar", "credentials": {"client_id": "nexus-fake-client",
            "client_secret": "test-fake-provider-secret", "refresh_token": "test-fake-provider-secret"},
            "config": {"calendar_id": "primary"}}  # fmt: skip
    r = h.client.post("/api/v1/integrations/connections", headers=auth(user, tenant, **{"Idempotency-Key": "g"}),
                      json=body)  # fmt: skip
    assert r.status_code == 201 and r.json()["status"] == "active", r.text
    assert r.json()["account_masked"] == "d…@example.test" and r.json()["config"] == {"calendar_id": "primary"}
    bad = {**body, "credentials": {**body["credentials"], "refresh_token": "wrong-refresh-token-xyz"}}
    other_tenant = uuid.uuid4()
    r2 = h.client.post("/api/v1/integrations/connections",
                       headers=auth(h.user(other_tenant, MANAGE), other_tenant, **{"Idempotency-Key": "g2"}),
                       json=bad)  # fmt: skip
    assert r2.json()["status"] == "error" and r2.json()["last_error"] == "invalid_credentials"

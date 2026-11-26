"""Provider connections: Vault-only secrets, references-only rows, no secrets in logs or responses, rotation
with rollback, revocation, resolution contract (build.md §19, §22 items 10–16)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from integration_service.jobs import rotation_reminders
from nexus_core.clients import ServiceClient
from nexus_core.credentials import CredentialResolver
from nexus_core.testing import auth
from nexus_core.vault import VaultClient, tenant_secret_path

from .conftest import MANAGE, READ, TWILIO, VAULT, VAULT_TOKEN, make_settings

pytestmark = [pytest.mark.integration, pytest.mark.security]
SECRETS = list(TWILIO.values())


def _all_secret_free(text: str, secrets: list[str]) -> bool:
    return not any(s in text for s in secrets)


# ------------------------------------------------------------------------------------------- create
def test_credential_is_written_to_vault_and_never_returned_stored_or_logged(harness, tenant, caplog):
    caplog.set_level(logging.DEBUG)
    r = harness.create_twilio(tenant)
    assert r.status_code == 201, r.text
    body = r.json()
    connection_id = body["id"]
    path = tenant_secret_path("test", str(tenant), "twilio", connection_id)
    assert body["status"] == "active" and body["vault_ref"] == path and body["health"] == "healthy"
    assert body["account_masked"] == "AC…cdef" and body["last_validated_at"]
    assert harness.vault.latest(path) == TWILIO  # 1. in Vault
    assert _all_secret_free(r.text, SECRETS)  # 2. not in the response
    detail = harness.client.get(f"/api/v1/integrations/connections/{connection_id}",
                                headers=auth(harness.user(tenant, READ), tenant))  # fmt: skip
    listing = harness.client.get("/api/v1/integrations/connections", headers=auth(harness.user(tenant, READ), tenant))
    assert _all_secret_free(detail.text + listing.text, SECRETS)
    assert _all_secret_free(harness.dump_everything([tenant]), SECRETS)  # 3. rows hold references only
    assert _all_secret_free(caplog.text, [*SECRETS, VAULT_TOKEN])  # 4. never logged
    # Audit + change events carry ids/enums only.
    events = harness.outbox("integration.connection.changed")
    mine = [e for e in events if e["payload"]["connection_id"] == connection_id]
    assert mine and mine[-1]["payload"] == {"connection_id": connection_id, "provider": "twilio", "status": "active"}


def test_validation_errors_never_echo_values(harness, tenant):
    user = harness.user(tenant, MANAGE)
    bad = {"account_sid": "not-a-sid-ZZZ-unique-marker", "auth_token": "short"}
    r = harness.client.post("/api/v1/integrations/connections",
                            headers=auth(user, tenant, **{"Idempotency-Key": "v1"}),
                            json={"provider": "twilio", "credentials": bad})  # fmt: skip
    assert r.status_code == 422
    assert "unique-marker" not in r.text and "short" not in r.text
    assert {f["field"] for f in r.json()["fields"]} == {"account_sid", "auth_token"}
    assert harness.vault.writes == []
    unknown = harness.client.post("/api/v1/integrations/connections",
                                  headers=auth(user, tenant, **{"Idempotency-Key": "v2"}),
                                  json={"provider": "telnyx", "credentials": {"k": "v"}})  # fmt: skip
    assert unknown.status_code == 422


def test_failed_connection_test_is_recorded_without_provider_body(harness, tenant):
    harness.mock.get(f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO['account_sid']}.json").mock(
        return_value=httpx.Response(401, json={"message": "Authenticate " + TWILIO["auth_token"]})
    )
    user = harness.user(tenant, MANAGE)
    r = harness.client.post("/api/v1/integrations/connections",
                            headers=auth(user, tenant, **{"Idempotency-Key": "f1"}),
                            json={"provider": "twilio", "credentials": TWILIO})  # fmt: skip
    assert r.status_code == 201 and r.json()["status"] == "error" and r.json()["last_error"] == "invalid_credentials"
    assert _all_secret_free(r.text, SECRETS)
    assert harness.client.get("/internal/v1/connections/resolve", params={"provider": "twilio"},
                              headers=harness.internal(tenant)).status_code == 404  # fmt: skip


def test_create_requires_step_up_permission_and_is_idempotent(harness, tenant):
    no_mfa = harness.user(tenant, MANAGE, mfa=False)
    reader = harness.user(tenant, READ)
    body = {"provider": "twilio", "credentials": TWILIO}
    r = harness.client.post("/api/v1/integrations/connections", headers=auth(no_mfa, tenant, **{"Idempotency-Key": "a"}),
                            json=body)  # fmt: skip
    assert r.status_code == 401 and r.json()["code"] == "step_up_required"
    r = harness.client.post("/api/v1/integrations/connections", headers=auth(reader, tenant, **{"Idempotency-Key": "a"}),
                            json=body)  # fmt: skip
    assert r.status_code == 403
    assert harness.vault.writes == []
    first = harness.create_twilio(tenant, key="same-key")
    again = harness.create_twilio(tenant, key="same-key")
    assert first.status_code == again.status_code == 201 and first.json()["id"] == again.json()["id"]
    assert len(harness.vault.writes) == 1  # the replay did not write again
    other = dict(TWILIO, auth_token="0" * 32)
    mismatch = harness.create_twilio(tenant, key="same-key", creds=other)
    assert mismatch.status_code == 422
    # The idempotency row stores a keyed fingerprint, not the credential.
    assert _all_secret_free(harness.dump_everything([tenant]), [*SECRETS, "0" * 32])
    duplicate = harness.create_twilio(tenant, key="other-key")
    assert duplicate.status_code == 409 and len(harness.vault.writes) == 1


def test_vault_outage_fails_closed_without_metadata(harness, tenant):
    harness.vault.down = True
    r = harness.create_twilio(tenant)
    assert r.status_code == 503 and r.json()["code"] == "secrets_unavailable"
    assert harness.sql("SELECT count(*) AS n FROM provider_connections", tenant_id=tenant)[0]["n"] == 0


# ------------------------------------------------------------------------------------------- rotate
def test_rotation_switches_only_after_validation(harness, tenant):
    conn = harness.create_twilio(tenant).json()
    path = conn["vault_ref"]
    new = dict(TWILIO, auth_token="abcdef0123456789" * 2)
    user = harness.user(tenant, MANAGE)
    r = harness.client.post(f"/api/v1/integrations/connections/{conn['id']}/rotate", headers=auth(user, tenant),
                            json={"credentials": new})  # fmt: skip
    assert r.status_code == 200, r.text
    assert r.json()["vault_version"] == 2 and r.json()["status"] == "active"
    assert harness.vault.latest(path) == new
    assert f"{path}-next" in harness.vault.destroyed and f"{path}-next" not in harness.vault.data
    assert _all_secret_free(r.text, [new["auth_token"]])
    actions = [
        e["payload"]["action"] for e in harness.outbox("audit.event") if e["payload"]["resource_id"] == conn["id"]
    ]
    assert "integration.connection.rotated" in actions


def test_rotation_rolls_back_when_validation_fails(harness, tenant):
    conn = harness.create_twilio(tenant).json()
    path = conn["vault_ref"]
    bad = dict(TWILIO, auth_token="1111111111111111" * 2)
    harness.mock.get(f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO['account_sid']}.json").mock(
        return_value=httpx.Response(401, json={})
    )
    user = harness.user(tenant, MANAGE)
    r = harness.client.post(f"/api/v1/integrations/connections/{conn['id']}/rotate", headers=auth(user, tenant),
                            json={"credentials": bad})  # fmt: skip
    assert r.status_code == 422 and r.json()["rolled_back"] is True and r.json()["error"] == "invalid_credentials"
    assert harness.vault.latest(path) == TWILIO and len(harness.vault.data[path]) == 1  # previous version live
    assert f"{path}-next" not in harness.vault.data  # the candidate was deleted
    row = harness.sql("SELECT status, vault_version, last_error FROM provider_connections WHERE id = :i",
                      {"i": conn["id"]}, tenant_id=tenant)[0]  # fmt: skip
    assert (row["status"], row["vault_version"], row["last_error"]) == ("active", 1, "rotation_validation_failed")
    failure = [e for e in harness.outbox("audit.event")
               if e["payload"]["resource_id"] == conn["id"] and e["payload"]["outcome"] == "failure"]  # fmt: skip
    assert failure and failure[0]["payload"]["metadata"]["rolled_back"] is True
    # Still resolvable with the old reference.
    ok = harness.client.get("/internal/v1/connections/resolve", params={"provider": "twilio"},
                            headers=harness.internal(tenant))  # fmt: skip
    assert ok.status_code == 200 and ok.json()["vault_ref"] == path


def test_rotate_and_revoke_require_step_up(harness, tenant):
    conn = harness.create_twilio(tenant).json()
    no_mfa = harness.user(tenant, MANAGE, mfa=False)
    for path, body in ((f"{conn['id']}/rotate", {"credentials": TWILIO}), (f"{conn['id']}/revoke", None)):
        r = harness.client.post(f"/api/v1/integrations/connections/{path}", headers=auth(no_mfa, tenant), json=body)
        assert r.status_code == 401, path


# ------------------------------------------------------------------------------------------- revoke / disable
def test_revoked_connection_is_no_longer_resolvable_and_vault_is_wiped(harness, tenant):
    conn = harness.create_twilio(tenant).json()
    user = harness.user(tenant, MANAGE)
    params = {"provider": "twilio"}
    assert harness.client.get("/internal/v1/connections/resolve", params=params,
                              headers=harness.internal(tenant)).status_code == 200  # fmt: skip
    r = harness.client.post(f"/api/v1/integrations/connections/{conn['id']}/revoke", headers=auth(user, tenant))
    assert r.status_code == 200 and r.json()["status"] == "revoked" and r.json()["last_vault_status"] == "deleted"
    assert conn["vault_ref"] in harness.vault.destroyed and conn["vault_ref"] not in harness.vault.data
    assert harness.client.get("/internal/v1/connections/resolve", params=params,
                              headers=harness.internal(tenant)).status_code == 404  # fmt: skip
    assert any(e["payload"] == {"connection_id": conn["id"], "provider": "twilio", "status": "revoked"}
               for e in harness.outbox("integration.connection.changed"))  # fmt: skip
    assert harness.client.post(f"/api/v1/integrations/connections/{conn['id']}/test",
                               headers=auth(user, tenant)).status_code == 409  # fmt: skip
    # A new connection can be created after revocation.
    assert harness.create_twilio(tenant).status_code == 201


def test_disable_blocks_resolution_and_enable_revalidates(harness, tenant):
    conn = harness.create_twilio(tenant).json()
    user = harness.user(tenant, MANAGE)
    params = {"provider": "twilio"}
    r = harness.client.post(f"/api/v1/integrations/connections/{conn['id']}/disable", headers=auth(user, tenant))
    assert r.json()["status"] == "disabled"
    assert harness.client.get("/internal/v1/connections/resolve", params=params,
                              headers=harness.internal(tenant)).status_code == 404  # fmt: skip
    r = harness.client.post(f"/api/v1/integrations/connections/{conn['id']}/enable", headers=auth(user, tenant))
    assert r.json()["status"] == "active"
    assert harness.client.get("/internal/v1/connections/resolve", params=params,
                              headers=harness.internal(tenant)).status_code == 200  # fmt: skip


# ------------------------------------------------------------------------------------------- resolve
def test_resolve_contract_and_credential_resolver_end_to_end(harness, tenant, sdb):
    conn = harness.create_twilio(tenant).json()
    r = harness.client.get("/internal/v1/connections/resolve", params={"provider": "twilio"},
                           headers=harness.internal(tenant))  # fmt: skip
    assert r.json() == {"connection_id": conn["id"], "provider": "twilio", "status": "active",
                        "vault_ref": conn["vault_ref"], "config": {}}  # fmt: skip
    assert harness.sql("SELECT last_retrieval_at FROM secret_registry WHERE vault_ref = :v",
                       {"v": conn["vault_ref"]})[0]["last_retrieval_at"] is not None  # fmt: skip
    # The shared CredentialResolver (as used by call-service) resolves the reference, then reads Vault itself.
    token = harness.tokens.service("svc-call-service")

    async def resolve() -> dict[str, str]:
        async def tok() -> str:
            return token

        client = ServiceClient("integration-service", "http://integration.test", tok,
                               transport=httpx.ASGITransport(app=harness.app))  # fmt: skip
        resolver = CredentialResolver(make_settings(sdb, service_name="call-service"),
                                      VaultClient(VAULT, token=VAULT_TOKEN), client)  # fmt: skip
        creds = await resolver.resolve(tenant, "twilio")
        return {"source": creds.source, "connection_id": creds.connection_id, "sid": creds.secret("account_sid")}

    out = harness.run(resolve())
    assert out == {"source": "tenant", "connection_id": conn["id"], "sid": TWILIO["account_sid"]}


def test_resolve_accepts_only_allowlisted_services_with_tenant(harness, tenant):
    harness.create_twilio(tenant)
    params = {"provider": "twilio"}
    user = harness.user(tenant, MANAGE)
    assert harness.client.get("/internal/v1/connections/resolve", params=params,
                              headers=auth(user, tenant)).status_code == 403  # fmt: skip
    assert harness.client.get("/internal/v1/connections/resolve", params=params,
                              headers=harness.internal(tenant, "svc-unknown")).status_code == 403  # fmt: skip
    assert harness.client.get("/internal/v1/connections/resolve", params=params,
                              headers=harness.internal(None)).status_code == 403  # fmt: skip
    assert harness.client.get("/internal/v1/connections/resolve", params=params,
                              headers=harness.internal(uuid.uuid4())).status_code == 404  # fmt: skip


# ------------------------------------------------------------------------------------------- isolation
def test_cross_tenant_isolation(harness):
    a, b = uuid.uuid4(), uuid.uuid4()
    conn = harness.create_twilio(a).json()
    hb = auth(harness.user(b, MANAGE), b)
    assert harness.client.get("/api/v1/integrations/connections", headers=hb).json()["items"] == []
    assert harness.client.get(f"/api/v1/integrations/connections/{conn['id']}", headers=hb).status_code == 404
    for action, body in (("test", None), ("rotate", {"credentials": TWILIO}), ("disable", None), ("revoke", None)):
        r = harness.client.post(f"/api/v1/integrations/connections/{conn['id']}/{action}", headers=hb, json=body)
        assert r.status_code == 404, action
    assert harness.client.get("/internal/v1/connections/resolve", params={"provider": "twilio"},
                              headers=harness.internal(b)).status_code == 404  # fmt: skip
    assert harness.sql("SELECT count(*) AS n FROM provider_connections", tenant_id=b)[0]["n"] == 0
    assert harness.vault.latest(conn["vault_ref"]) == TWILIO  # untouched
    assert harness.client.get("/api/v1/integrations/connections",
                              headers=auth(harness.user(a, MANAGE), b)).status_code == 403  # fmt: skip


def test_permission_boundaries(harness, tenant):
    conn = harness.create_twilio(tenant).json()
    nobody = auth(harness.user(tenant, {"bookings:read"}), tenant)
    reader = auth(harness.user(tenant, READ), tenant)
    assert harness.client.get("/api/v1/integrations/connections", headers=nobody).status_code == 403
    assert harness.client.get("/api/v1/integrations/providers", headers=nobody).status_code == 403
    assert harness.client.get("/api/v1/integrations/connections", headers=reader).status_code == 200
    for action in ("test", "disable", "enable", "revoke"):
        assert harness.client.post(f"/api/v1/integrations/connections/{conn['id']}/{action}",
                                   headers=reader).status_code == 403, action  # fmt: skip
    for path in ("/api/v1/platform/secrets", "/api/v1/platform/integrations/connections"):
        assert harness.client.get(path, headers=auth(harness.user(tenant, MANAGE))).status_code == 403


# ------------------------------------------------------------------------------------------- platform
def test_platform_secrets_view_lists_references_never_values(harness, tenant):
    conn = harness.create_twilio(tenant).json()
    operator = harness.platform_user({"platform:secrets:read"})
    r = harness.client.get("/api/v1/platform/secrets", params={"tenant_id": str(tenant)}, headers=auth(operator))
    assert r.status_code == 200
    item = next(i for i in r.json()["items"] if i["vault_ref"] == conn["vault_ref"])
    assert item["owning_service"] == "integration-service" and item["environment"] == "test"
    assert item["health"] == "healthy" and item["kind"] == "provider_credential"
    assert _all_secret_free(r.text, SECRETS)
    assert harness.client.post("/api/v1/platform/secrets/health-check", headers=auth(operator)).status_code == 403
    manager = harness.platform_user({"platform:secrets:manage"})
    assert harness.client.post("/api/v1/platform/secrets/health-check", headers=auth(manager)).json()["checked"] >= 1


def test_platform_connection_uses_the_credential_resolver_default_path(harness):
    admin = harness.platform_user({"platform:integrations:read", "platform:integrations:manage"})
    harness.mock.get("https://api.anthropic.com/v1/models").mock(return_value=httpx.Response(200, json={"data": []}))
    key = "sk-ant-" + "a1B2" * 10
    ok = harness.client.post("/api/v1/platform/integrations/connections", headers=auth(admin),
                             json={"provider": "anthropic", "owning_service": "ai-gateway",
                                   "credentials": {"api_key": key}})  # fmt: skip
    assert ok.status_code == 201, ok.text
    assert ok.json()["vault_ref"] == "test/platform/ai-gateway/anthropic" and ok.json()["status"] == "active"
    assert harness.vault.latest("test/platform/ai-gateway/anthropic") == {"api_key": key}
    assert key not in ok.text
    wrong = harness.client.post("/api/v1/platform/integrations/connections", headers=auth(admin),
                                json={"provider": "anthropic", "owning_service": "crm-service",
                                      "credentials": {"api_key": key}})  # fmt: skip
    assert wrong.status_code == 422
    no_mfa = harness.platform_user({"platform:integrations:manage"}, mfa=False)
    r = harness.client.post(f"/api/v1/platform/integrations/connections/{ok.json()['id']}/revoke",
                            headers=auth(no_mfa))  # fmt: skip
    assert r.status_code == 401
    r = harness.client.post(f"/api/v1/platform/integrations/connections/{ok.json()['id']}/revoke", headers=auth(admin))
    assert r.json()["status"] == "revoked" and "test/platform/ai-gateway/anthropic" not in harness.vault.data
    assert any(e["tenant_id"] is None and e["payload"]["status"] == "revoked"
               for e in harness.outbox("integration.connection.changed"))  # fmt: skip


def test_rotation_reminders_are_sent_once(harness, tenant):
    conn = harness.create_twilio(tenant).json()
    harness.sql("UPDATE secret_registry SET next_rotation_due = :d WHERE vault_ref = :v",
                {"d": datetime.now(UTC) + timedelta(days=2), "v": conn["vault_ref"]})  # fmt: skip
    assert harness.run(rotation_reminders(harness.app)) >= 1
    assert harness.run(rotation_reminders(harness.app)) == 0
    mine = [e for e in harness.outbox("notification.requested") if e["payload"]["resource_id"] == conn["id"]]
    assert len(mine) == 1
    assert mine[0]["payload"]["permission"] == "integrations:manage" and mine[0]["tenant_id"] == str(tenant)


def test_secretstr_inputs_are_not_exposed_by_models():
    from integration_service.api.models import ConnectionCreate

    model = ConnectionCreate(provider="twilio", credentials={"auth_token": SecretStr("x" * 32)})
    assert "x" * 32 not in repr(model) and "x" * 32 not in model.model_dump_json()

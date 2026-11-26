"""Outbound webhooks (SSRF corpus, signing, replay, retries, no redirects, DNS rebinding, consumers with
duplicate/forged events) and inbound HubSpot webhooks (signature, replay window, dedupe)."""

from __future__ import annotations

import json
import socket
import time
import uuid

import httpx
import pytest

from integration_service.api.inbound import hubspot_signature
from integration_service.webhooks import DeliveryDeps, deliver, fan_out, sweep
from nexus_core.messaging import EventConsumer
from nexus_core.testing import auth
from nexus_core.webhooks import WebhookVerificationError, verify_signature
from nexus_events import ActorRef, make_event

from .conftest import MANAGE, PUBLIC, READ, SECRET

pytestmark = [pytest.mark.integration, pytest.mark.security]
HOOK_SECRET = "whsec_" + "k3y" * 12
TARGET = "https://93.184.216.34/hooks/nexus"

SSRF_CORPUS = [
    "http://93.184.216.34/hook",  # plain HTTP
    "https://127.0.0.1/hook",
    "https://localhost/hook",
    "https://169.254.169.254/latest/meta-data/",  # cloud metadata
    "https://10.0.0.5/hook",
    "https://172.16.3.4/hook",
    "https://192.168.1.10/hook",
    "https://100.64.0.1/hook",  # CGNAT
    "https://0.0.0.0/hook",
    "https://[::1]/hook",
    "https://[fd00::1]/hook",  # IPv6 ULA
    "https://[fe80::1]/hook",
    "https://[::ffff:127.0.0.1]/hook",  # IPv4-mapped loopback
    "https://user:pass@93.184.216.34/hook",  # credentials in URL
    "https://2130706433/hook",  # decimal-encoded 127.0.0.1
    "https://0x7f000001/hook",
    "https://metadata.google.internal/computeMetadata/v1/",
    "file:///etc/passwd",
    "gopher://93.184.216.34:70/_",
    "ftp://93.184.216.34/",
    "https:///nohost",
    "https://203.0.113.10/hook",  # TEST-NET-3 (reserved)
]


def _endpoint(h, tenant, url=TARGET, events=("booking.*",), *, user=None):
    user = user or h.user(tenant, MANAGE)
    return h.client.post("/api/v1/webhooks/endpoints", headers=auth(user, tenant),
                         json={"url": url, "event_types": list(events), "secret": HOOK_SECRET})  # fmt: skip


def _event(tenant, event_type="booking.created", producer="booking-service", event_id=None):
    payload = {"booking_id": str(uuid.uuid4()), "contact_id": str(uuid.uuid4()), "status": "tentative"}
    if event_type == "lead.created":
        payload = {"contact_id": str(uuid.uuid4())}
    env = make_event(event_type, producer=producer, actor=ActorRef(type="system", id="test"),
                     correlation_id=uuid.uuid4(), tenant_id=tenant, payload=payload)  # fmt: skip
    return env.model_copy(update={"event_id": event_id}) if event_id else env


def _fan_out(h, event):
    async def go() -> None:
        async with h.app.state.db.tenant_session(event.tenant_id) as s:
            await fan_out(event, s)

    h.run(go())


def _deps(h):
    return DeliveryDeps(h.app.state.db, h.app.state.secrets, h.app.state.settings)


def _deliver_all(h, tenant):
    """Sweep, then run the published tasks of this test's tenant (other tests share the database)."""
    h.published.clear()
    h.run(sweep(h.app.state.db, _publish(h)))
    mine = [(ctx, did) for ctx, did in list(h.published) if ctx.tenant_id == tenant]
    h.published[:] = mine
    return [h.run(deliver(_deps(h), ctx, uuid.UUID(did))) for ctx, did in mine]


def _publish(h):
    async def publish(ctx, delivery_id):
        h.published.append((ctx, delivery_id))

    return publish


# ------------------------------------------------------------------------------------------- registration
@pytest.mark.parametrize("url", SSRF_CORPUS)
def test_ssrf_corpus_is_rejected_at_registration(harness, tenant, url):
    r = _endpoint(harness, tenant, url)
    assert r.status_code == 422, (url, r.text)
    assert harness.vault.writes == []


def test_registration_stores_the_secret_in_vault_only(harness, tenant):
    r = _endpoint(harness, tenant)
    assert r.status_code == 201, r.text
    body = r.json()
    assert HOOK_SECRET not in r.text and "secret" not in body
    assert body["event_types"] == ["booking.canceled", "booking.confirmed", "booking.created"]
    assert harness.vault.latest(body["secret_ref"]) == {"signing_secret": HOOK_SECRET}
    assert HOOK_SECRET not in harness.dump_everything()
    listing = harness.client.get("/api/v1/webhooks/endpoints", headers=auth(harness.user(tenant, READ), tenant))
    assert HOOK_SECRET not in listing.text and len(listing.json()["items"]) == 1
    assert _endpoint(harness, tenant, events=("not.a.event",)).status_code == 422


def test_endpoint_management_permissions_and_step_up(harness, tenant):
    reader = harness.user(tenant, READ)
    no_mfa = harness.user(tenant, MANAGE, mfa=False)
    assert _endpoint(harness, tenant, user=reader).status_code == 403
    assert _endpoint(harness, tenant, user=no_mfa).status_code == 401
    ep = _endpoint(harness, tenant).json()
    patch = {"url": "https://127.0.0.1/x", "expected_version": ep["version"]}
    r = harness.client.patch(f"/api/v1/webhooks/endpoints/{ep['id']}", headers=auth(harness.user(tenant, MANAGE), tenant),
                             json=patch)  # fmt: skip
    assert r.status_code == 422  # revalidated on change
    assert harness.client.delete(f"/api/v1/webhooks/endpoints/{ep['id']}",
                                 headers=auth(reader, tenant)).status_code == 403  # fmt: skip
    gone = harness.client.delete(f"/api/v1/webhooks/endpoints/{ep['id']}",
                                 headers=auth(harness.user(tenant, MANAGE), tenant))  # fmt: skip
    assert gone.status_code == 204 and ep["secret_ref"] not in harness.vault.data
    assert harness.client.get(f"/api/v1/webhooks/endpoints/{ep['id']}", headers=auth(reader, tenant)).status_code == 404


# ------------------------------------------------------------------------------------------- delivery
def test_delivery_is_signed_and_verifiable_and_duplicates_do_nothing(harness, tenant):
    ep = _endpoint(harness, tenant).json()
    seen: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    harness.mock.post(TARGET).mock(side_effect=receive)
    event = _event(tenant)
    _fan_out(harness, event)
    _fan_out(harness, event)  # duplicate delivery of the same event
    assert _deliver_all(harness, tenant) == ["succeeded"]
    assert len(seen) == 1
    req = seen[0]
    assert req.headers["x-nexus-event-id"] == str(event.event_id)
    assert req.headers["host"] == "93.184.216.34" and req.headers["content-type"] == "application/json"
    ts = verify_signature(HOOK_SECRET.encode(), req.content, req.headers["x-nexus-signature"])
    assert abs(ts - time.time()) < 60
    assert json.loads(req.content)["payload"] == event.payload
    # Replay protection on the receiving side: a stale or altered request fails verification.
    with pytest.raises(WebhookVerificationError, match="tolerance"):
        verify_signature(HOOK_SECRET.encode(), req.content, req.headers["x-nexus-signature"], now=time.time() + 3600)
    with pytest.raises(WebhookVerificationError, match="mismatch"):
        verify_signature(HOOK_SECRET.encode(), req.content + b" ", req.headers["x-nexus-signature"])
    # Duplicate tasks for a finished delivery do nothing.
    ctx, did = harness.published[0]
    assert harness.run(deliver(_deps(harness), ctx, uuid.UUID(did))) == "skipped"
    detail = harness.client.get(f"/api/v1/webhooks/endpoints/{ep['id']}/deliveries/{did}",
                                headers=auth(harness.user(tenant, READ), tenant)).json()  # fmt: skip
    assert detail["status"] == "succeeded" and [a["outcome"] for a in detail["attempt_log"]] == ["succeeded"]


def test_retries_back_off_then_fail_and_emit_event(harness, tenant):
    harness.app.state.settings.webhook_max_attempts = 3
    ep = _endpoint(harness, tenant).json()
    route = harness.mock.post(TARGET).mock(return_value=httpx.Response(503))
    _fan_out(harness, _event(tenant))
    assert _deliver_all(harness, tenant) == ["queued"]
    row = harness.sql("SELECT attempts, next_attempt_at > now() AS later, last_error FROM webhook_deliveries",
                      tenant_id=tenant)[0]  # fmt: skip
    assert (row["attempts"], row["later"], row["last_error"]) == (1, True, "http_503")
    assert _deliver_all(harness, tenant) == []  # not due yet: nothing is published
    for _ in range(2):
        harness.sql("UPDATE webhook_deliveries SET next_attempt_at = now()", tenant_id=tenant)
        _deliver_all(harness, tenant)
    final = harness.sql("SELECT id, status, attempts FROM webhook_deliveries", tenant_id=tenant)[0]
    assert (final["status"], final["attempts"]) == ("failed", 3) and route.call_count == 3
    failed = [e for e in harness.outbox("webhook.delivery.failed") if e["payload"]["delivery_id"] == str(final["id"])]
    assert failed[0]["payload"] == {"endpoint_id": ep["id"], "delivery_id": str(final["id"]), "attempts": 3}
    assert harness.sql("SELECT consecutive_failures FROM webhook_endpoints", tenant_id=tenant)[0][
        "consecutive_failures"] == 1  # fmt: skip
    # The attempt log is append-only.
    with pytest.raises(Exception, match="permission denied"):  # no DELETE grant (and an append-only trigger)
        harness.sql("DELETE FROM webhook_delivery_attempts", tenant_id=tenant)
    redo = harness.client.post(f"/api/v1/webhooks/endpoints/{ep['id']}/deliveries/{final['id']}/redeliver",
                               headers=auth(harness.user(tenant, MANAGE), tenant))  # fmt: skip
    assert redo.status_code == 200 and redo.json()["status"] == "queued"


def test_redirects_are_never_followed(harness, tenant):
    _endpoint(harness, tenant)
    harness.mock.post(TARGET).mock(return_value=httpx.Response(302, headers={"location": "http://169.254.169.254/"}))
    metadata = harness.mock.route(url__startswith="http://169.254.169.254").mock(return_value=httpx.Response(200))
    _fan_out(harness, _event(tenant))
    assert _deliver_all(harness, tenant) == ["queued"]
    assert metadata.call_count == 0
    assert harness.sql("SELECT last_error FROM webhook_deliveries", tenant_id=tenant)[0]["last_error"] == (
        "redirect_not_followed"
    )


def test_destination_is_revalidated_at_delivery_dns_rebinding(harness, tenant, monkeypatch):
    real = socket.getaddrinfo
    answers = {"hooks.customer.test": "93.184.216.34"}

    def fake_getaddrinfo(host, *args, **kwargs):
        if host in answers:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], 443))]
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert _endpoint(harness, tenant, "https://hooks.customer.test/in").status_code == 201
    route = harness.mock.post("https://93.184.216.34/in").mock(return_value=httpx.Response(200))
    _fan_out(harness, _event(tenant))
    answers["hooks.customer.test"] = "127.0.0.1"  # the attacker rebinds DNS after registration
    assert _deliver_all(harness, tenant) == ["failed"]
    assert route.call_count == 0
    row = harness.sql("SELECT last_error FROM webhook_deliveries", tenant_id=tenant)[0]
    assert row["last_error"] == "unsafe_destination"
    attempts = harness.sql("SELECT outcome FROM webhook_delivery_attempts", tenant_id=tenant)
    assert [a["outcome"] for a in attempts] == ["blocked"]
    # A pinned delivery goes to the validated IP while keeping the hostname for Host/SNI.
    answers["hooks.customer.test"] = "93.184.216.34"
    seen: list[httpx.Request] = []
    route.mock(side_effect=lambda r: seen.append(r) or httpx.Response(200))
    _fan_out(harness, _event(tenant))
    assert _deliver_all(harness, tenant) == ["succeeded"]
    assert seen[0].url.host == "93.184.216.34" and seen[0].headers["host"] == "hooks.customer.test"


def test_consumer_ignores_forged_producers_and_dedupes(harness, tenant):
    """Runs the real EventConsumer message handler with the service's handlers and producer allowlist."""
    from integration_service.tables import inbox
    from integration_service.webhooks import EVENT_HANDLERS, SUBSCRIBABLE

    _endpoint(harness, tenant, events=("booking.created", "lead.created"))
    consumer = EventConsumer(consumer_name="integration_service", db=harness.app.state.db, inbox=inbox,
                             amqp_url="amqp://unused", handlers=dict(EVENT_HANDLERS),
                             allowed_producers={k: set(v) for k, v in SUBSCRIBABLE.items()})  # fmt: skip

    class Message:
        def __init__(self, event):
            self.body = event.model_dump_json().encode()
            self.headers: dict = {}
            self.acked = False

        async def ack(self):
            self.acked = True

        async def reject(self, requeue=False):
            raise AssertionError("unexpected reject")

    forged = _event(tenant, producer="crm-service")  # crm-service may not emit booking.*
    legit = _event(tenant)
    lead = _event(tenant, "lead.created", producer="crm-service")
    for event in (forged, legit, legit, lead):
        msg = Message(event)
        harness.run(consumer.handle_message(msg))
        assert msg.acked
    rows = harness.sql("SELECT event_id FROM webhook_deliveries", tenant_id=tenant)
    assert {str(r["event_id"]) for r in rows} == {str(legit.event_id), str(lead.event_id)}


def test_cross_tenant_isolation(harness):
    a, b = uuid.uuid4(), uuid.uuid4()
    ep = _endpoint(harness, a).json()
    hb = auth(harness.user(b, MANAGE), b)
    assert harness.client.get("/api/v1/webhooks/endpoints", headers=hb).json()["items"] == []
    for method, path, body in (
        ("get", f"/api/v1/webhooks/endpoints/{ep['id']}", None),
        ("get", f"/api/v1/webhooks/endpoints/{ep['id']}/deliveries", None),
        ("patch", f"/api/v1/webhooks/endpoints/{ep['id']}", {"status": "disabled", "expected_version": 1}),
        ("post", f"/api/v1/webhooks/endpoints/{ep['id']}/rotate-secret", {"secret": "x" * 40}),
        ("delete", f"/api/v1/webhooks/endpoints/{ep['id']}", None),
    ):  # fmt: skip
        kwargs = {"json": body} if body is not None else {}
        assert getattr(harness.client, method)(path, headers=hb, **kwargs).status_code == 404, path
    # Tenant B's events never reach tenant A's endpoint.
    _fan_out(harness, _event(b))
    assert harness.sql("SELECT count(*) AS n FROM webhook_deliveries", tenant_id=a)[0]["n"] == 0
    assert harness.vault.latest(ep["secret_ref"]) == {"signing_secret": HOOK_SECRET}


def test_secret_rotation_signs_with_the_new_secret(harness, tenant):
    ep = _endpoint(harness, tenant).json()
    new = "whsec_" + "n3w" * 12
    r = harness.client.post(f"/api/v1/webhooks/endpoints/{ep['id']}/rotate-secret",
                            headers=auth(harness.user(tenant, MANAGE), tenant), json={"secret": new})  # fmt: skip
    assert r.status_code == 200 and new not in r.text and r.json()["secret_version"] == 2
    seen: list[httpx.Request] = []
    harness.mock.post(TARGET).mock(side_effect=lambda req: seen.append(req) or httpx.Response(200))
    _fan_out(harness, _event(tenant))
    _deliver_all(harness, tenant)
    verify_signature(new.encode(), seen[0].content, seen[0].headers["x-nexus-signature"])
    with pytest.raises(WebhookVerificationError):
        verify_signature(HOOK_SECRET.encode(), seen[0].content, seen[0].headers["x-nexus-signature"])


# ------------------------------------------------------------------------------------------- inbound HubSpot
def _hubspot_post(h, events, *, secret=SECRET, ts=None, signature=None):
    body = json.dumps(events).encode()
    ts = ts or str(int(time.time() * 1000))
    sig = signature or hubspot_signature(secret, "POST", f"{PUBLIC}/webhooks/hubspot", body, ts)
    return h.client.post("/webhooks/hubspot", content=body, headers={
        "content-type": "application/json", "x-hubspot-request-timestamp": ts, "x-hubspot-signature-v3": sig})  # fmt: skip


def test_inbound_hubspot_signature_replay_and_dedupe(fake_harness, tenant):
    h = fake_harness
    connection_id = uuid.uuid4()
    h.sql("INSERT INTO inbound_webhook_registry (provider, external_account_id, tenant_id, connection_id) "
          "VALUES ('hubspot', '4242', :t, :c)", {"t": tenant, "c": connection_id})  # fmt: skip
    events = [{"eventId": 1001, "subscriptionType": "contact.creation", "portalId": 4242, "objectId": 7,
               "occurredAt": int(time.time() * 1000)},
              {"eventId": 1002, "subscriptionType": "contact.creation", "portalId": 999, "objectId": 8}]  # fmt: skip
    assert _hubspot_post(h, events).status_code == 204
    assert _hubspot_post(h, events).status_code == 204  # redelivery: deduped
    rows = h.sql("SELECT provider_event_id, connection_id FROM inbound_webhook_events", tenant_id=tenant)
    assert [r["provider_event_id"] for r in rows] == ["1001"] and str(rows[0]["connection_id"]) == str(connection_id)
    assert _hubspot_post(h, events, secret="wrong-secret").status_code == 401
    stale = str(int((time.time() - 600) * 1000))
    assert _hubspot_post(h, events, ts=stale).status_code == 401
    assert h.client.post("/webhooks/hubspot", content=b"[]").status_code == 401  # unsigned
    bad = _hubspot_post(h, {"not": "a list"})
    assert bad.status_code == 400

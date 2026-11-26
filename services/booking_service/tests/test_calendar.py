"""Calendar adapter contract (real adapter code against the simulator), busy-time subtraction, idempotent
external writes with reconcile-after-ambiguous-failure, and real-mode token refresh mocked with respx."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

import httpx
import pytest
import respx
from pydantic import SecretStr

from booking_service.providers.calendar import (
    CalendarAuthError,
    CalendarUnavailable,
    GoogleCalendarProvider,
    GoogleTokenSource,
)
from booking_service.service import sync_calendars
from nexus_core.credentials import ResolvedCredentials
from nexus_core.testing import auth
from nexus_providers.calendar import CalendarEvent

from .conftest import MANAGE, SECRET, SIM_BASE, iso, simulator_app, slot_start

API = f"{SIM_BASE}/google_calendar/calendar/v3"


def _sim() -> tuple[GoogleCalendarProvider, httpx.ASGITransport]:
    transport = httpx.ASGITransport(app=simulator_app())
    return GoogleCalendarProvider(API, transport=transport), transport


async def _token(transport: httpx.ASGITransport) -> str:
    class Resolver:
        async def resolve(self, tenant_id, provider, **kw):
            return ResolvedCredentials(provider, None, {"fake_secret": SecretStr(SECRET)}, {}, "fake")

        def invalidate(self, *a):
            return None

    source = GoogleTokenSource(Resolver(), f"{SIM_BASE}/google_calendar/token", transport=transport)  # type: ignore[arg-type]
    return await source.access_token(uuid.uuid4())


# ------------------------------------------------------------------------------------------- adapter contract
async def test_adapter_contract_against_simulator():
    cal, transport = _sim()
    token = await _token(transport)
    start = slot_start(3, 10)
    event = CalendarEvent(summary="Appointment", start=start, end=start + timedelta(minutes=30),
                          idempotency_key="nc" + "a" * 40)  # fmt: skip
    eid = await cal.create_event(token, "cal-1", event)
    assert eid == event.idempotency_key
    assert await cal.create_event(token, "cal-1", event) == eid  # duplicate insert → 409 → same id
    busy = await cal.busy(token, "cal-1", start - timedelta(hours=1), start + timedelta(hours=1))
    assert [(b.start, b.end) for b in busy] == [(start, start + timedelta(minutes=30))]
    assert await cal.event_exists(token, "cal-1", eid) is True
    await cal.cancel_event(token, "cal-1", eid)
    await cal.cancel_event(token, "cal-1", eid)  # idempotent delete (410)
    assert await cal.event_exists(token, "cal-1", eid) is False
    assert await cal.busy(token, "cal-1", start - timedelta(hours=1), start + timedelta(hours=1)) == []


async def test_adapter_maps_errors_without_leaking_bodies():
    cal, _ = _sim()
    with pytest.raises(CalendarAuthError):
        await cal.busy("not-a-valid-token", "cal", slot_start(3, 0), slot_start(3, 1))
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{API}/freeBusy").mock(return_value=httpx.Response(503, text="token=abc secret body"))
        real = GoogleCalendarProvider(API)
        with pytest.raises(CalendarUnavailable) as exc:
            await real.busy("t", "cal", slot_start(3, 0), slot_start(3, 1))
        assert "secret" not in str(exc.value) and str(exc.value) == "http_503"
        mock.post(f"{API}/freeBusy").mock(side_effect=httpx.ConnectTimeout("boom"))
        with pytest.raises(CalendarUnavailable):
            await real.busy("t", "cal", slot_start(3, 0), slot_start(3, 1))


async def test_real_mode_refreshes_the_token_from_vault_resolved_credentials(caplog):
    """Real Google token endpoint mocked with respx; the refresh token comes from the resolver (Vault)."""

    class VaultResolver:
        calls = 0

        async def resolve(self, tenant_id, provider, **kw):
            VaultResolver.calls += 1
            values = {"client_id": SecretStr("cid.apps.googleusercontent.com"),
                      "client_secret": SecretStr("GOCSPX-supersecret"), "refresh_token": SecretStr("1//refresh-xyz")}  # fmt: skip
            return ResolvedCredentials(provider, "conn-1", values, {}, "tenant")

        def invalidate(self, *a):
            return None

    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(200, json={"access_token": "ya29.live-token", "expires_in": 3599})
        )
        source = GoogleTokenSource(VaultResolver(), "https://oauth2.googleapis.com/token")  # type: ignore[arg-type]
        tenant = uuid.uuid4()
        assert await source.access_token(tenant) == "ya29.live-token"
        assert await source.access_token(tenant) == "ya29.live-token"  # cached in memory
        assert route.call_count == 1 and VaultResolver.calls == 1
        form = dict(x.split("=", 1) for x in route.calls[0].request.content.decode().split("&"))
        assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "1%2F%2Frefresh-xyz"
        route.mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
        with pytest.raises(CalendarAuthError):
            await source.access_token(uuid.uuid4())
    text = caplog.text
    assert "GOCSPX-supersecret" not in text and "1//refresh-xyz" not in text and "ya29.live-token" not in text


# ------------------------------------------------------------------------------------------- service level


@pytest.mark.integration
def test_availability_subtracts_external_busy_and_fails_closed(harness, tenant):
    from fake_providers.routers import google_calendar as sim

    rid = harness.resource(tenant, calendar=True)
    cal_id = next(r for r in harness.client.get("/api/v1/bookings/resources",
                                                headers=auth(harness.user(tenant, MANAGE), tenant)).json()["items"]
                  if r["id"] == rid)["calendar_id"]  # fmt: skip
    start = slot_start(3, 9)
    sim.STATE.busy.setdefault(cal_id, []).append((iso(start), iso(start + timedelta(minutes=60))))
    params = {"from": iso(start), "to": iso(start + timedelta(hours=2))}
    slots = harness.client.get("/internal/v1/availability", params=params, headers=harness.internal(tenant)).json()
    assert [s["start"][11:16] for s in slots["slots"]] == ["10:00", "10:30"]
    # Booking into external busy time is refused.
    body = {"contact_id": str(uuid.uuid4()), "start": iso(start), "end": iso(start + timedelta(minutes=30)),
            "source": {"type": "ai_tool", "id": "e"}, "resource_id": rid}  # fmt: skip
    r = harness.client.post("/internal/v1/bookings/tentative", headers=harness.internal(tenant, "b1"), json=body)
    assert r.status_code == 409
    # Calendar down → no slots for that resource, and the UI is told why.
    harness.transport.down = True
    user = harness.user(tenant, MANAGE)
    harness.app.state.calendar_tokens._cache.clear()
    pub = harness.client.get("/api/v1/availability", params=params, headers=auth(user, tenant)).json()
    assert pub["slots"] == [] and pub["calendar_unavailable"] == [rid]
    r = harness.client.post("/internal/v1/bookings/tentative", headers=harness.internal(tenant, "b2"),
                            json={**body, "start": iso(start + timedelta(hours=1)),
                                  "end": iso(start + timedelta(minutes=90))})  # fmt: skip
    assert r.status_code == 503


@pytest.mark.integration
def test_confirmed_bookings_sync_idempotently_and_reconcile_after_ambiguous_writes(harness, tenant):
    from fake_providers.routers import google_calendar as sim

    rid = harness.resource(tenant, calendar=True)
    contact = uuid.uuid4()
    start = slot_start(4, 11)
    body = {"contact_id": str(contact), "start": iso(start), "end": iso(start + timedelta(minutes=30)),
            "source": {"type": "ai_tool", "id": "e"}, "resource_id": rid}  # fmt: skip
    booking_id = harness.client.post("/internal/v1/bookings/tentative", headers=harness.internal(tenant, "t"),
                                     json=body).json()["booking_id"]  # fmt: skip
    assert harness.run(sync_calendars(harness.app)) == 0  # tentative holds are not written externally
    harness.client.post(f"/internal/v1/bookings/{booking_id}/confirm", headers=harness.internal(tenant, "c"),
                        json={"contact_id": str(contact)})  # fmt: skip

    # First attempt: the insert reaches Google but the response is lost (ambiguous).
    harness.transport.ambiguous_inserts = 1
    assert harness.run(sync_calendars(harness.app)) == 1
    state = harness.sql("SELECT * FROM calendar_sync_state WHERE appointment_id = :i", {"i": booking_id},
                        tenant_id=tenant)[0]  # fmt: skip
    assert state["state"] == "ambiguous" and state["last_error"] == "calendar_unavailable"
    events = [e for (c, _), e in sim.STATE.events.items() if e["status"] == "confirmed"]
    assert len(events) == 1  # it did happen
    # Retry: reconcile first (GET finds the event), so no second insert is attempted.
    harness.sql("UPDATE calendar_sync_state SET next_attempt_at = now() WHERE appointment_id = :i", {"i": booking_id},
                tenant_id=tenant)  # fmt: skip
    before = [r for r in harness.transport.requests if r[0] == "POST" and r[1].endswith("/events")]
    assert harness.run(sync_calendars(harness.app)) == 1
    after = [r for r in harness.transport.requests if r[0] == "POST" and r[1].endswith("/events")]
    assert len(after) == len(before)
    state = harness.sql("SELECT * FROM calendar_sync_state WHERE appointment_id = :i", {"i": booking_id},
                        tenant_id=tenant)[0]  # fmt: skip
    assert state["state"] == "synced" and state["current_event_id"] == state["desired_event_id"]
    appt = harness.sql("SELECT external_event_id FROM appointments WHERE id = :i", {"i": booking_id},
                       tenant_id=tenant)[0]  # fmt: skip
    assert appt["external_event_id"] == state["current_event_id"]
    assert harness.run(sync_calendars(harness.app)) == 0  # nothing left to do

    # Reschedule moves the event (old deleted, new id created); cancel removes it.
    user = harness.user(tenant, MANAGE)
    version = harness.client.get(f"/api/v1/bookings/{booking_id}", headers=auth(user, tenant)).json()["version"]
    moved = harness.client.post(f"/api/v1/bookings/{booking_id}/reschedule",
                                headers=auth(user, tenant, **{"Idempotency-Key": "mv"}),
                                json={"start": iso(start + timedelta(hours=1)),
                                      "end": iso(start + timedelta(minutes=90)), "expected_version": version})  # fmt: skip
    assert moved.status_code == 200, moved.text
    harness.run(sync_calendars(harness.app))
    live = [e for e in sim.STATE.events.values() if e["status"] == "confirmed"]
    assert len(live) == 1 and live[0]["start"]["dateTime"].startswith(iso(start + timedelta(hours=1))[:16])
    harness.client.post(f"/api/v1/bookings/{booking_id}/cancel", headers=auth(user, tenant, **{"Idempotency-Key": "x"}),
                        json={"reason": "requested"})  # fmt: skip
    harness.run(sync_calendars(harness.app))
    assert [e for e in sim.STATE.events.values() if e["status"] == "confirmed"] == []
    final = harness.sql("SELECT state, current_event_id FROM calendar_sync_state WHERE appointment_id = :i",
                        {"i": booking_id}, tenant_id=tenant)[0]  # fmt: skip
    assert final["state"] == "synced" and final["current_event_id"] is None


@pytest.mark.integration
def test_auth_failures_are_permanent(harness, tenant):
    rid = harness.resource(tenant, calendar=True)
    user = harness.user(tenant, MANAGE)
    start = slot_start(5, 10)
    bk = harness.client.post("/api/v1/bookings", headers=auth(user, tenant, **{"Idempotency-Key": "p"}),
                             json={"contact_id": str(uuid.uuid4()), "start": iso(start),
                                   "end": iso(start + timedelta(minutes=30)), "resource_id": rid}).json()  # fmt: skip

    class Broken:
        async def access_token(self, tenant_id):
            raise CalendarAuthError("token_http_400")

    real = harness.app.state.calendar_tokens
    harness.app.state.calendar_tokens = Broken()
    try:
        harness.run(sync_calendars(harness.app))
    finally:
        harness.app.state.calendar_tokens = real
    row = harness.sql("SELECT state, last_error FROM calendar_sync_state WHERE appointment_id = :i", {"i": bk["id"]},
                      tenant_id=tenant)[0]  # fmt: skip
    assert (row["state"], row["last_error"]) == ("failed", "calendar_auth_failed")

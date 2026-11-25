"""Unit tests (no Docker): template escaping and link validation, and the SSE permission filter."""

from __future__ import annotations

import json
import uuid

import pytest

from nexus_core.errors import Unprocessable
from nexus_core.realtime import ui_channel
from notification_service.domain import Viewer, notify_channel, relay
from notification_service.templates import TEMPLATES, render, validate_link

BASE = "https://app.nexus.test"
INVITE = {
    "tenant_name": "Acme",
    "inviter": "Ada",
    "roles": "Analyst",
    "link": f"{BASE}/invite/accept?token=abc",
    "expires_at": "2026-12-01T00:00:00Z",
}


def test_html_escapes_every_variable_and_text_keeps_plain_text():
    hostile = {**INVITE, "tenant_name": '<script>alert(1)</script>"&', "inviter": "<img src=x onerror=y>"}
    out = render("invitation", hostile, public_base_url=BASE)
    assert "<script>" not in out.html and "&lt;script&gt;alert(1)&lt;/script&gt;&quot;&amp;" in out.html
    assert "<img" not in out.html and "&lt;img src=x onerror=y&gt;" in out.html
    assert "<script>alert(1)</script>" in out.text  # text/plain is never rendered as HTML
    assert out.subject == "You're invited to join <script>alert(1)</script>\"& on NexusCall"
    assert f'href="{BASE}/invite/accept?token=abc"' in out.html and INVITE["link"] in out.text


def test_link_in_href_cannot_break_out_of_the_attribute():
    out = render("draft_ready", {"link": f'{BASE}/inbox?x="onmouseover="y'}, public_base_url=BASE)
    assert 'onmouseover="y' not in out.html and "&quot;onmouseover=&quot;y" in out.html


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.test/invite",
        "javascript:alert(1)",
        "/invite/accept",
        f"{BASE}.evil.test/x",
        f"{BASE}@evil.test/x",
        "http://app.nexus.test/invite",  # scheme downgrade
        f"{BASE}/a b",
        f"{BASE}\\@evil.test",
        f"{BASE}:8443/x",
    ],
)
def test_links_must_point_at_the_public_base_url(link):
    with pytest.raises(Unprocessable):
        validate_link(link, BASE)
    with pytest.raises(Unprocessable):
        render("invitation", {**INVITE, "link": link}, public_base_url=BASE)


def test_valid_links_including_a_base_path():
    assert validate_link(f"{BASE}/x?y=1#z", BASE)
    assert validate_link("https://ex.test/app/inbox", "https://ex.test/app/")
    with pytest.raises(Unprocessable):
        validate_link("https://ex.test/application", "https://ex.test/app")


def test_variables_are_strictly_checked():
    with pytest.raises(Unprocessable):
        render("invitation", {k: v for k, v in INVITE.items() if k != "roles"}, public_base_url=BASE)
    with pytest.raises(Unprocessable):
        render("invitation", {**INVITE, "bcc": "x@y.test"}, public_base_url=BASE)
    with pytest.raises(Unprocessable):  # CR/LF would allow header injection through the subject
        render("invitation", {**INVITE, "tenant_name": "Acme\r\nBcc: x@evil.test"}, public_base_url=BASE)
    with pytest.raises(Unprocessable):
        render("invitation", {**INVITE, "inviter": "x" * 501}, public_base_url=BASE)
    with pytest.raises(Unprocessable):
        render("no_such_template", {}, public_base_url=BASE)


def test_every_template_renders_text_and_html_with_its_required_variables():
    expected = {
        "invitation",
        "support_access_requested",
        "support_access_active",
        "support_access_rejected",
        "draft_ready",
        "handoff_requested",
        "generic",
    }
    assert set(TEMPLATES) == expected
    for key, template in TEMPLATES.items():
        values = {name: (f"{BASE}/x" if name == "link" else f"v-{name}") for name in template.required}
        out = render(key, values, public_base_url=BASE)
        assert out.subject and out.text.strip() and out.html.startswith("<!doctype html>")
        assert "$" not in out.subject + out.text + out.html


# --------------------------------------------------------------------------- SSE relay filter
T = uuid.UUID(int=7)
ME = uuid.UUID(int=8)


def _ui(event, permission, data=None):
    return json.dumps({"type": event, "permission": permission, "data": data or {"call_id": "c1"}})


def _note(nid, *, user=None, permission=None, kind="draft_ready"):
    return json.dumps({"id": str(nid), "user_id": str(user) if user else None, "permission": permission, "kind": kind})


def test_ui_events_are_forwarded_only_with_the_events_permission():
    v = Viewer(T, ME, frozenset({"calls:read"}))
    ui = ui_channel(T)
    assert relay(ui, _ui("call.updated", "calls:read"), v) == {
        "type": "ui",
        "event": "call.updated",
        "data": {"call_id": "c1"},
    }
    assert relay(ui, _ui("call.updated", "calls:recordings:read"), v) is None
    assert relay(ui, json.dumps({"type": "call.updated", "data": {}}), v) is None  # no permission tag: dropped
    assert relay(ui, _ui("call.updated", ""), v) is None
    assert relay(ui, "not json", v) is None
    assert relay(ui, _ui("call.updated", "calls:read", {"n": "x" * 65}), v) is None
    assert relay(ui_channel(uuid.uuid4()), _ui("call.updated", "calls:read"), v) is None  # other tenant
    wildcard = Viewer(T, ME, frozenset({"calls:*"}))
    assert relay(ui, _ui("call.updated", "calls:recordings:read"), wildcard) is not None
    assert relay(ui, _ui("lead.updated", "leads:read"), wildcard) is None


def test_notification_hints_are_forwarded_only_to_their_audience():
    v = Viewer(T, ME, frozenset({"emails:drafts:review"}), frozenset({"import_completed"}))
    ch = notify_channel(T)
    nid = uuid.uuid4()
    assert relay(ch, _note(nid, user=ME), v) == {"type": "notification", "id": str(nid)}
    assert relay(ch, _note(nid, user=uuid.uuid4()), v) is None
    assert relay(ch, _note(nid, permission="emails:drafts:review"), v) == {"type": "notification", "id": str(nid)}
    assert relay(ch, _note(nid, permission="calls:manage"), v) is None
    assert relay(ch, _note(nid, user=ME, kind="import_completed"), v) is None  # muted kind
    assert relay(ch, json.dumps({"id": "nope", "user_id": str(ME)}), v) is None
    assert relay(notify_channel(uuid.uuid4()), _note(nid, user=ME), v) is None

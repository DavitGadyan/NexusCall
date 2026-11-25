"""Agents, append-only prompt versions, activation, internal agent contracts and permission boundaries."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration
BASE = "/api/v1/ai/agents"
PROMPT = "You are the phone assistant for a plumbing company. Keep answers short and friendly."


def _create(h, tenant_id, headers=None, **body):
    payload = {"name": f"a-{uuid.uuid4().hex[:6]}", "kind": "voice", "config": {"system_prompt": PROMPT}, **body}
    return h.client.post(BASE, json=payload, headers={**(headers or h.user(tenant_id)), **h.ik()})


def test_agent_lifecycle_versions_and_activation(harness, tenants):
    a, _ = tenants
    headers = harness.user(a)
    resp = _create(
        harness,
        a,
        headers,
        config={
            "system_prompt": PROMPT,
            "greeting": "Hello!",
            "tool_ids": ["knowledge.search", "booking.find_slots"],
            "max_call_seconds": 300,
        },
    )
    assert resp.status_code == 201, resp.text
    agent = resp.json()
    assert agent["status"] == "draft" and agent["current_version"] is None and agent["latest_version"] == 1

    v2 = harness.client.post(
        f"{BASE}/{agent['id']}/versions",
        json={"system_prompt": PROMPT + " Mention our free quotes.", "change_note": "quotes"},
        headers={**headers, **harness.ik()},
    )
    assert v2.status_code == 201 and v2.json()["version"] == 2
    versions = harness.client.get(f"{BASE}/{agent['id']}/versions", headers=headers).json()["items"]
    assert [v["version"] for v in versions] == [2, 1]

    act = harness.client.post(f"{BASE}/{agent['id']}/versions/2/activate", headers=headers)
    assert act.status_code == 200 and act.json()["status"] == "active" and act.json()["current_version"] == 2
    audit = [e["payload"] for e in harness.outbox_events("audit.event")]
    activated = [p for p in audit if p["action"] == "ai.agent.version_activated"]
    assert activated and activated[0]["metadata"]["version"] == 2
    assert harness.client.post(f"{BASE}/{agent['id']}/versions/9/activate", headers=headers).status_code == 404

    stale = harness.client.patch(f"{BASE}/{agent['id']}", json={"name": "Renamed", "version": 1}, headers=headers)
    assert stale.status_code == 409
    current = harness.client.get(f"{BASE}/{agent['id']}", headers=headers).json()
    ok = harness.client.patch(
        f"{BASE}/{agent['id']}", json={"name": "Renamed", "version": current["version"]}, headers=headers
    )
    assert ok.status_code == 200 and ok.json()["name"] == "Renamed"

    archived = harness.client.delete(f"{BASE}/{agent['id']}", headers=headers)
    assert archived.status_code == 200 and archived.json()["status"] == "archived"
    assert (
        harness.client.post(
            f"{BASE}/{agent['id']}/versions", json={"system_prompt": PROMPT}, headers={**headers, **harness.ik()}
        ).status_code
        == 409
    )


def test_prompt_versions_are_append_only_in_the_database(harness, tenants):
    a, _ = tenants
    agent = harness.make_agent(a, "voice")

    async def tamper(database):
        errors = []
        for stmt in (
            "UPDATE prompt_versions SET system_prompt = 'evil' WHERE agent_id = :id",
            "DELETE FROM prompt_versions WHERE agent_id = :id",
        ):
            try:
                async with database.tenant_session(a) as s:
                    await s.execute(text(stmt), {"id": agent["id"]})
            except Exception as exc:  # noqa: BLE001 - asserting the database refuses
                errors.append(str(exc))
        return errors

    errors = harness.sql(tamper)
    assert len(errors) == 2 and all("append-only" in e or "permission denied" in e for e in errors)


def test_create_agent_idempotency(harness, tenants):
    a, _ = tenants
    headers = {**harness.user(a), **harness.ik()}
    body = {"name": "Idem agent", "kind": "email", "config": {"system_prompt": PROMPT}}
    first = harness.client.post(BASE, json=body, headers=headers)
    replay = harness.client.post(BASE, json=body, headers=headers)
    assert first.status_code == replay.status_code == 201 and first.json()["id"] == replay.json()["id"]
    assert harness.client.post(BASE, json={**body, "name": "Other"}, headers=headers).status_code == 422
    assert harness.client.post(BASE, json=body, headers=harness.user(a)).status_code == 422  # key required
    dup = harness.client.post(BASE, json=body, headers={**harness.user(a), **harness.ik()})
    assert dup.status_code == 409


@pytest.mark.security
@pytest.mark.parametrize(
    ("kind", "config", "detail"),
    [
        ("email", {"tool_ids": ["knowledge.search"]}, "tool_not_allowed"),  # drafting never uses tools
        ("voice", {"tool_ids": ["booking.confirm"]}, "tool_not_allowed"),  # approval-gated tool not for voice
        ("voice", {"tool_ids": ["admin.grant_access"]}, "tool_not_allowed"),
        ("voice", {"knowledge_source_ids": [str(uuid.uuid4())]}, "unknown_knowledge_source"),
    ],
)
def test_agents_get_narrow_tool_sets_only(harness, tenants, kind, config, detail):
    a, _ = tenants
    resp = _create(harness, a, kind=kind, config={"system_prompt": PROMPT, **config})
    assert resp.status_code == 422 and resp.json()["code_detail"] == detail


@pytest.mark.security
@pytest.mark.parametrize(
    "secret",
    ["Use api_key=sk-live-abcdefghijklmnopqrstuvwx", "password: hunter2hunter2", "postgres://app:pw@db:5432/x"],
)
def test_credentials_are_rejected_in_prompts(harness, tenants, secret):
    a, _ = tenants
    resp = _create(harness, a, config={"system_prompt": f"{PROMPT} {secret}"})
    assert resp.status_code == 422


def test_knowledge_sources_must_belong_to_the_tenant(harness, tenants):
    a, b = tenants
    foreign = harness.make_source(b)
    resp = _create(harness, a, config={"system_prompt": PROMPT, "knowledge_source_ids": [foreign["id"]]})
    assert resp.status_code == 422


@pytest.mark.security
def test_agent_permission_boundaries(harness, tenants):
    a, _ = tenants
    agent = harness.make_agent(a, "voice")
    reader = harness.user(a, {"ai:agents:read"})
    nobody = harness.user(a, {"ai:usage:read"})
    assert harness.client.get(BASE, headers=reader).status_code == 200
    assert harness.client.get(BASE, headers=nobody).status_code == 403
    assert harness.client.get(f"{BASE}/{agent['id']}", headers=nobody).status_code == 403
    assert _create(harness, a, reader).status_code == 403
    assert (
        harness.client.patch(f"{BASE}/{agent['id']}", json={"name": "x1", "version": 1}, headers=reader).status_code
        == 403
    )
    assert harness.client.post(f"{BASE}/{agent['id']}/versions/1/activate", headers=reader).status_code == 403
    assert harness.client.delete(f"{BASE}/{agent['id']}", headers=reader).status_code == 403
    # A service account is not a tenant member: it cannot use the public configuration API at all.
    svc = harness.service("svc-voice-service", a)
    assert harness.client.post(f"{BASE}/{agent['id']}/versions/1/activate", headers=svc).status_code == 403
    assert harness.client.get(BASE).status_code == 401


@pytest.mark.security
def test_cross_tenant_agent_access_is_404(harness, tenants):
    a, b = tenants
    agent = harness.make_agent(a, "voice")
    hb = harness.user(b)
    assert harness.client.get(f"{BASE}/{agent['id']}", headers=hb).status_code == 404
    assert (
        harness.client.patch(f"{BASE}/{agent['id']}", json={"name": "x1", "version": 1}, headers=hb).status_code == 404
    )
    assert harness.client.delete(f"{BASE}/{agent['id']}", headers=hb).status_code == 404
    assert harness.client.get(f"{BASE}/{agent['id']}/versions", headers=hb).status_code == 404
    assert harness.client.get(f"{BASE}/{agent['id']}/versions/1", headers=hb).status_code == 404
    assert harness.client.post(f"{BASE}/{agent['id']}/versions/1/activate", headers=hb).status_code == 404
    assert (
        harness.client.post(
            f"{BASE}/{agent['id']}/versions", json={"system_prompt": PROMPT}, headers={**hb, **harness.ik()}
        ).status_code
        == 404
    )
    assert harness.client.get(BASE, headers=hb).json()["items"] == []
    svc = harness.service("svc-voice-service", b)
    assert harness.client.get(f"/internal/v1/agents/{agent['id']}/runtime-config", headers=svc).status_code == 404


# =========================================================================================== internal contracts
def test_internal_agent_and_runtime_config_contracts(harness, tenants):
    a, _ = tenants
    agent = harness.make_agent(
        a,
        "voice",
        greeting="Hi!",
        disclosure_text="This is an AI assistant.",
        voice_id="v1",
        tool_ids=["knowledge.search"],
    )
    info = harness.client.get(f"/internal/v1/agents/{agent['id']}", headers=harness.service("svc-campaign-service", a))
    assert info.status_code == 200
    assert info.json() == {"agent_id": agent["id"], "name": agent["name"], "kind": "voice", "status": "active"}
    cfg = harness.client.get(
        f"/internal/v1/agents/{agent['id']}/runtime-config", headers=harness.service("svc-voice-service", a)
    )
    assert cfg.status_code == 200
    body = cfg.json()
    assert set(body) == {
        "agent_id",
        "version",
        "kind",
        "system_prompt",
        "model",
        "voice_id",
        "tool_ids",
        "greeting",
        "disclosure_text",
        "escalation_rules",
        "max_call_seconds",
    }
    assert body["version"] == 1 and body["tool_ids"] == ["knowledge.search"] and body["disclosure_text"]
    # Only the listed callers; a draft agent has no runtime config.
    assert (
        harness.client.get(
            f"/internal/v1/agents/{agent['id']}/runtime-config", headers=harness.service("svc-campaign-service", a)
        ).status_code
        == 403
    )
    draft = harness.make_agent(a, "voice", activate=False)
    resp = harness.client.get(
        f"/internal/v1/agents/{draft['id']}/runtime-config", headers=harness.service("svc-call-service", a)
    )
    assert resp.status_code == 409
    assert (
        harness.client.get(
            f"/internal/v1/agents/{uuid.uuid4()}", headers=harness.service("svc-mcp-registry", a)
        ).status_code
        == 404
    )


@pytest.mark.security
def test_no_internal_endpoint_can_change_agent_configuration(harness, tenants):
    """build.md §24.6 rule 12: internal routes are reads plus search/drafting/postprocess only."""
    a, _ = tenants
    internal = {
        (method, route.path)
        for route in harness.app.routes
        if getattr(route, "path", "").startswith("/internal/v1/")
        for method in getattr(route, "methods", set())
    }
    allowed_posts = {"/internal/v1/knowledge/search", "/internal/v1/drafting/runs", "/internal/v1/calls/postprocess"}
    assert {p for m, p in internal if m != "GET"} == allowed_posts
    agent = harness.make_agent(a, "voice")
    svc = harness.service("svc-voice-service", a)
    for method in ("put", "patch", "delete"):
        resp = getattr(harness.client, method)(f"/internal/v1/agents/{agent['id']}", headers=svc)
        assert resp.status_code == 405
    assert (
        harness.client.post(
            f"/api/v1/ai/agents/{agent['id']}/versions", json={"system_prompt": PROMPT}, headers={**svc, **harness.ik()}
        ).status_code
        == 403
    )

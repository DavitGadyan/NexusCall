"""mcp-registry behaviour and security boundaries against a real database (build.md §24.3–24.11)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from mcp_registry.catalogue import FORBIDDEN_PROPERTIES, INTERNAL_TOOLS
from mcp_registry.consumers import on_security_event
from mcp_registry.tables import mcp_servers, outbox
from nexus_core.context import Actor
from nexus_events import ActorRef, make_event

pytestmark = [pytest.mark.integration, pytest.mark.security]

GATEWAY = "svc-ai-gateway"
EXECUTOR = "svc-tool-execution"
READ = {"ai:agents:read"}
MANAGE = {"ai:agents:read", "ai:agents:manage"}
SHA = "a" * 64


def _tenant() -> uuid.UUID:
    return uuid.uuid4()


def _grant(h, tenant, agent, tools, **kw):
    body = {"grants": [{"tool_id": t, **kw} for t in tools]}
    return h.client.put("/api/v1/ai/grants", params={"agent_id": str(agent)}, json=body, headers=h.user(tenant, MANAGE))


def _agent_tools(h, tenant, agent):
    r = h.client.get(f"/internal/v1/agents/{agent}/tools", headers=h.service(GATEWAY, tenant))
    assert r.status_code == 200, r.text
    return {t["tool_id"]: t for t in r.json()["tools"]}


def _check(h, tenant, agent, tool_id):
    return h.client.post(
        f"/internal/v1/tools/{tool_id}/grant-check", json={"agent_id": str(agent)}, headers=h.service(EXECUTOR, tenant)
    )


def _privileged_tool(h, tool_id="platform.manage_users"):
    async def go(db):
        async with db.global_session() as s:
            await s.execute(
                text(
                    "INSERT INTO tool_definitions (tool_id, display_name, description, kind, target, input_schema,"
                    " output_schema, risk_class, side_effecting, requires_approval, enabled) VALUES (:t, 'Manage users',"
                    " 'privileged', 'internal', '{}', CAST(:s AS jsonb), CAST(:s AS jsonb), 4, true, true, true)"
                    " ON CONFLICT (tool_id) DO NOTHING"
                ),
                {"t": tool_id, "s": '{"type":"object","properties":{},"additionalProperties":false}'},
            )

    h.sql(go)


def _set_tool(h, tool_id, **values):
    async def go(db):
        async with db.global_session() as s:
            sets = ", ".join(f"{k} = :{k}" for k in values)
            await s.execute(
                text(f"UPDATE tool_definitions SET {sets} WHERE tool_id = :tid"), {**values, "tid": tool_id}
            )

    h.sql(go)


# --------------------------------------------------------------------------- seeded catalogue
def test_seven_internal_tools_are_seeded_with_strict_schemas(harness):
    tenant = _tenant()
    r = harness.client.get("/api/v1/ai/tools", headers=harness.user(tenant, READ))
    assert r.status_code == 200, r.text
    items = {t["tool_id"]: t for t in r.json()["items"]}
    assert set(items) == {t["tool_id"] for t in INTERNAL_TOOLS}
    assert items["booking.confirm"]["risk_class"] == 3 and items["booking.confirm"]["requires_approval"] is True
    for tool in items.values():
        schema = tool["input_schema"]
        assert schema["additionalProperties"] is False
        assert not set(schema["properties"]) & FORBIDDEN_PROPERTIES
    assert r.json()["next_cursor"] is None
    page = harness.client.get("/api/v1/ai/tools", params={"limit": 3}, headers=harness.user(tenant, READ)).json()
    assert len(page["items"]) == 3 and page["next_cursor"]
    rest = harness.client.get(
        "/api/v1/ai/tools", params={"limit": 50, "cursor": page["next_cursor"]}, headers=harness.user(tenant, READ)
    ).json()
    assert len(rest["items"]) == 4


def test_tool_catalogue_requires_permission(harness):
    tenant = _tenant()
    assert harness.client.get("/api/v1/ai/tools", headers=harness.user(tenant, {"calls:read"})).status_code == 403
    assert harness.client.get("/api/v1/ai/tools").status_code == 401


# --------------------------------------------------------------------------- grants
def test_grants_validate_the_agent_and_class_rules(harness):
    tenant = _tenant()
    agent = harness.add_agent(tenant)
    # Unknown agent (or an agent of another tenant) cannot receive grants.
    other_tenant = _tenant()
    foreign = harness.add_agent(other_tenant)
    assert _grant(harness, tenant, uuid.uuid4(), ["knowledge.search"]).status_code == 404
    assert _grant(harness, tenant, foreign, ["knowledge.search"]).status_code == 404
    # Class 4 can never be granted.
    _privileged_tool(harness)
    r = _grant(harness, tenant, agent, ["knowledge.search", "platform.manage_users"])
    assert r.status_code == 422 and "class 4" in r.json()["detail"]
    # Class 3 always requires approval, even if the caller asks otherwise.
    r = _grant(harness, tenant, agent, ["knowledge.search", "booking.confirm"], requires_approval=False)
    assert r.status_code == 200, r.text
    items = {i["tool_id"]: i for i in r.json()["items"]}
    assert items["booking.confirm"]["requires_approval"] is True
    assert items["knowledge.search"]["effective_requires_approval"] is False
    # Unknown tool ids are rejected.
    assert _grant(harness, tenant, agent, ["no.such_tool"]).status_code == 422
    # Replace semantics: the second PUT removes booking.confirm.
    r = _grant(harness, tenant, agent, ["knowledge.search"])
    assert [i["tool_id"] for i in r.json()["items"]] == ["knowledge.search"]


def test_grant_changes_need_manage_permission_and_are_audited(harness):
    tenant = _tenant()
    agent = harness.add_agent(tenant)
    body = {"grants": [{"tool_id": "knowledge.search"}]}
    r = harness.client.put(
        "/api/v1/ai/grants", params={"agent_id": str(agent)}, json=body, headers=harness.user(tenant, READ)
    )
    assert r.status_code == 403
    r = harness.client.get("/api/v1/ai/grants", params={"agent_id": str(agent)}, headers=harness.user(tenant, READ))
    assert r.status_code == 403
    assert _grant(harness, tenant, agent, ["knowledge.search"]).status_code == 200

    async def audits(db):
        async with db.global_session() as s:
            rows = (await s.execute(select(outbox.c.envelope).where(outbox.c.tenant_id == tenant))).scalars().all()
        return [e["payload"]["action"] for e in rows if e["event_type"] == "audit.event"]

    assert "ai.tool_grants.updated" in harness.sql(audits)


def test_extra_fields_are_rejected_on_grants(harness):
    tenant = _tenant()
    agent = harness.add_agent(tenant)
    body = {"grants": [{"tool_id": "knowledge.search", "risk_class": 0}]}
    r = harness.client.put(
        "/api/v1/ai/grants", params={"agent_id": str(agent)}, json=body, headers=harness.user(tenant, MANAGE)
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- internal API
def test_agent_tools_returns_granted_enabled_tools_only(harness):
    tenant = _tenant()
    agent = harness.add_agent(tenant)
    _privileged_tool(harness)
    assert _grant(harness, tenant, agent, ["knowledge.search", "crm.add_note", "booking.confirm"]).status_code == 200
    tools = _agent_tools(harness, tenant, agent)
    assert set(tools) == {"knowledge.search", "crm.add_note", "booking.confirm"}
    assert tools["knowledge.search"]["input_schema"]["additionalProperties"] is False
    assert set(tools["crm.add_note"]) == {"tool_id", "version", "description", "input_schema", "risk_class"}
    # Another agent of the same tenant has nothing.
    assert _agent_tools(harness, tenant, harness.add_agent(tenant)) == {}
    # Tenant disables a tool: it disappears for every agent.
    r = harness.client.put(
        "/api/v1/ai/tools/settings",
        json={"settings": [{"tool_id": "crm.add_note", "enabled": False}]},
        headers=harness.user(tenant, {"tenant:manage"}),
    )
    assert r.status_code == 200, r.text
    assert "crm.add_note" not in _agent_tools(harness, tenant, agent)


def test_internal_endpoints_are_caller_restricted(harness):
    tenant = _tenant()
    agent = harness.add_agent(tenant)
    path = f"/internal/v1/agents/{agent}/tools"
    assert harness.client.get(path, headers=harness.service(EXECUTOR, tenant)).status_code == 403
    assert harness.client.get(path, headers=harness.service("svc-voice-service", tenant)).status_code == 403
    assert harness.client.get(path, headers=harness.user(tenant, MANAGE)).status_code == 403
    assert harness.client.get(path, headers=harness.service(GATEWAY, None)).status_code == 403  # tenant required
    check = "/internal/v1/tools/knowledge.search/grant-check"
    assert (
        harness.client.post(check, json={"agent_id": str(agent)}, headers=harness.service(GATEWAY, tenant)).status_code
        == 403
    )


def test_there_are_no_internal_mutation_endpoints(harness):
    internal = {
        (m, r.path) for r in harness.app.routes for m in getattr(r, "methods", ()) if r.path.startswith("/internal/")
    }
    writes = {(m, p) for m, p in internal if m not in ("GET", "HEAD")}
    assert writes == {("POST", "/internal/v1/tools/{tool_id}/grant-check")}


def test_grant_check_combines_every_layer(harness):
    tenant = _tenant()
    agent = harness.add_agent(tenant)
    _privileged_tool(harness)
    assert _check(harness, tenant, agent, "no.such_tool").status_code == 404
    assert _check(harness, tenant, agent, "http://169.254.169.254/latest").status_code in (404, 422)
    ungranted = _check(harness, tenant, agent, "crm.add_note").json()
    assert ungranted["enabled"] is True and ungranted["granted"] is False and ungranted["reason"] == "not_granted"
    assert _grant(harness, tenant, agent, ["crm.add_note", "booking.confirm"], calls_per_minute=7).status_code == 200
    ok = _check(harness, tenant, agent, "crm.add_note").json()
    assert ok["granted"] and ok["risk_class"] == 2 and ok["requires_approval"] is False
    assert ok["target"] == {
        "kind": "internal",
        "service": "crm-service",
        "method": "POST",
        "path": "/internal/v1/contacts/{contact_id}/notes",
    }
    assert ok["limits"]["rate_per_minute"] == 7 and ok["limits"]["agent_calls_per_minute"] == 60
    confirm = _check(harness, tenant, agent, "booking.confirm").json()
    assert confirm["granted"] and confirm["requires_approval"] is True

    # Privileged tools are never granted, whatever the database says.
    async def raw_grant(db):
        async with db.tenant_session(tenant) as s:
            await s.execute(
                text(
                    "INSERT INTO agent_tool_grants (tenant_id, agent_id, tool_id, granted_by) VALUES (:t, :a, :x, 'x')"
                ),
                {"t": tenant, "a": agent, "x": "platform.manage_users"},
            )

    harness.sql(raw_grant)
    priv = _check(harness, tenant, agent, "platform.manage_users").json()
    assert priv["granted"] is False and priv["reason"] == "privileged_tool"
    assert "platform.manage_users" not in _agent_tools(harness, tenant, agent)
    # Tenant tightening: approval added, rate lowered.
    r = harness.client.put(
        "/api/v1/ai/tools/settings",
        json={"settings": [{"tool_id": "crm.add_note", "requires_approval": True, "rate_per_minute": 3}]},
        headers=harness.user(tenant, {"tenant:manage"}),
    )
    assert r.status_code == 200
    tightened = _check(harness, tenant, agent, "crm.add_note").json()
    assert tightened["requires_approval"] is True and tightened["limits"]["rate_per_minute"] == 3


def test_platform_disable_and_revoke_take_effect_immediately(harness):
    tenant = _tenant()
    agent = harness.add_agent(tenant)
    assert _grant(harness, tenant, agent, ["booking.find_slots"]).status_code == 200
    admin = harness.admin()
    r = harness.client.post(
        "/api/v1/platform/ai/tools/booking.find_slots/disable", json={"reason": "incident 42"}, headers=admin
    )
    assert r.status_code == 200, r.text
    assert _check(harness, tenant, agent, "booking.find_slots").json()["reason"] == "tool_disabled"
    assert "booking.find_slots" not in _agent_tools(harness, tenant, agent)
    assert harness.client.post("/api/v1/platform/ai/tools/booking.find_slots/enable", headers=admin).status_code == 200
    assert _check(harness, tenant, agent, "booking.find_slots").json()["granted"] is True


def test_tenant_settings_can_only_tighten(harness):
    tenant = _tenant()
    hdr = harness.user(tenant, {"tenant:manage"})
    r = harness.client.put(
        "/api/v1/ai/tools/settings",
        json={"settings": [{"tool_id": "booking.confirm", "requires_approval": False}]},
        headers=hdr,
    )
    assert r.status_code == 422
    r = harness.client.put(
        "/api/v1/ai/tools/settings",
        json={"settings": [{"tool_id": "crm.add_note", "rate_per_minute": 5000}]},
        headers=hdr,
    )
    assert r.status_code == 422 and "platform limit" in r.json()["detail"]
    assert harness.client.get("/api/v1/ai/tools/settings", headers=harness.user(tenant, MANAGE)).status_code == 403
    items = {i["tool_id"]: i for i in harness.client.get("/api/v1/ai/tools/settings", headers=hdr).json()["items"]}
    assert items["booking.confirm"]["platform_requires_approval"] is True


# --------------------------------------------------------------------------- tenant isolation
def test_grants_and_settings_are_tenant_isolated(harness):
    tenant_a, tenant_b = _tenant(), _tenant()
    agent = harness.add_agent(tenant_a)
    harness.add_agent(tenant_b, agent)  # same agent id claimed in tenant B
    assert _grant(harness, tenant_a, agent, ["knowledge.search"]).status_code == 200
    assert "knowledge.search" in _agent_tools(harness, tenant_a, agent)
    assert _agent_tools(harness, tenant_b, agent) == {}
    assert _check(harness, tenant_b, agent, "knowledge.search").json()["granted"] is False
    r = harness.client.get("/api/v1/ai/grants", params={"agent_id": str(agent)}, headers=harness.user(tenant_b, MANAGE))
    assert r.status_code == 200 and r.json()["items"] == []
    # A member of tenant A cannot act in tenant B.
    sub_headers = harness.user(tenant_a, MANAGE)
    sub_headers["X-Tenant-ID"] = str(tenant_b)
    assert (
        harness.client.get("/api/v1/ai/grants", params={"agent_id": str(agent)}, headers=sub_headers).status_code == 403
    )

    async def probe(db):
        async with db.tenant_session(tenant_b) as s:
            seen = (
                await s.execute(text("SELECT count(*) FROM agent_tool_grants WHERE tenant_id = :t"), {"t": tenant_a})
            ).scalar_one()
        async with db.global_session() as s:
            none = (await s.execute(text("SELECT count(*) FROM agent_tool_grants"))).scalar_one()
        async with db.tenant_session(tenant_b) as s:
            try:
                await s.execute(
                    text(
                        "INSERT INTO agent_tool_grants (tenant_id, agent_id, tool_id, granted_by) VALUES (:t, :a, 'knowledge.search', 'x')"
                    ),
                    {"t": tenant_a, "a": uuid.uuid4()},
                )
                wrote = True
            except Exception:  # noqa: BLE001 - RLS WITH CHECK violation expected
                wrote = False
        return seen, none, wrote

    seen, none, wrote = harness.sql(probe)
    assert seen == 0 and none == 0 and wrote is False


# --------------------------------------------------------------------------- MCP servers (platform)
def _server_body(key="vendor-docs", **over):
    body = {
        "key": key,
        "display_name": "Vendor docs search",
        "owner": "platform-team",
        "purpose": "Search the vendor's public product documentation",
        "endpoint": "https://mcp.vendor.example/mcp",
        "environment": "test",
        "trust_level": "verified",
        "service_identity": f"mcp-{key}",
        "vault_ref": f"test/platform/mcp-servers/{key}",
        "allowed_hosts": ["mcp.vendor.example"],
    }
    body.update(over)
    return body


def test_server_registration_needs_manage_and_step_up(harness):
    body = _server_body(key=f"s{uuid.uuid4().hex[:6]}")
    r = harness.client.post("/api/v1/platform/ai/mcp-servers", json=body, headers=harness.admin({"platform:ai:read"}))
    assert r.status_code == 403
    r = harness.client.post("/api/v1/platform/ai/mcp-servers", json=body, headers=harness.admin(mfa=False))
    assert r.status_code == 401 and r.json()["code"] == "step_up_required"
    tenant = _tenant()
    assert (
        harness.client.post(
            "/api/v1/platform/ai/mcp-servers", json=body, headers=harness.user(tenant, MANAGE, mfa=True)
        ).status_code
        == 403
    )
    assert (
        harness.client.post(
            "/api/v1/platform/ai/mcp-servers", json=body, headers=harness.service(GATEWAY, None)
        ).status_code
        == 403
    )
    r = harness.client.post("/api/v1/platform/ai/mcp-servers", json=body, headers=harness.admin())
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["enabled"] is False and out["vault_ref"] == body["vault_ref"]
    assert "secret" not in r.text.lower() and "token" not in r.text.lower()
    # Tenants see approved servers without endpoints, identities or credential references.
    tenant_view = harness.client.get("/api/v1/ai/mcp-servers", headers=harness.user(tenant, READ))
    assert tenant_view.status_code == 200
    mine = [s for s in tenant_view.json()["items"] if s["key"] == body["key"]]
    assert mine and set(mine[0]) == {"id", "key", "display_name", "purpose", "trust_level", "health", "enabled"}
    assert harness.client.get("/api/v1/ai/mcp-servers", headers=harness.user(tenant, {"calls:read"})).status_code == 403


SSRF_CORPUS = [
    ("http://mcp.vendor.example/mcp", ["mcp.vendor.example"]),  # plain http
    ("https://10.0.0.5/mcp", ["10.0.0.5"]),  # RFC 1918
    ("https://169.254.169.254/latest/meta-data", ["169.254.169.254"]),  # cloud metadata
    ("https://127.0.0.1:8443/mcp", ["127.0.0.1"]),  # loopback
    ("https://[::1]/mcp", ["::1"]),  # IPv6 loopback (also not a valid allowed host)
    ("https://localhost/mcp", ["localhost"]),  # resolves to loopback
    ("https://rebind.vendor.example/mcp", ["rebind.vendor.example"]),  # public name, private address
    ("https://metadata.vendor.example/mcp", ["metadata.vendor.example"]),  # name pointing at metadata
    ("https://mcp.vendor.example/mcp", ["docs.vendor.example"]),  # host not on the allowlist
    ("https://user:pw@mcp.vendor.example/mcp", ["mcp.vendor.example"]),  # credentials in URL
    ("https://mcp.vendor.example/mcp?next=http://10.0.0.1", ["mcp.vendor.example"]),  # query smuggling
    ("file:///etc/passwd", ["mcp.vendor.example"]),
    ("https://unknown.example/mcp", ["unknown.example"]),  # does not resolve
]


@pytest.mark.parametrize(("endpoint", "hosts"), SSRF_CORPUS)
def test_ssrf_corpus_is_rejected_at_registration(harness, endpoint, hosts):
    body = _server_body(key=f"s{uuid.uuid4().hex[:6]}", endpoint=endpoint, allowed_hosts=hosts)
    r = harness.client.post("/api/v1/platform/ai/mcp-servers", json=body, headers=harness.admin())
    assert r.status_code == 422, (endpoint, r.text)


def test_server_credentials_are_dedicated_references(harness):
    key = f"s{uuid.uuid4().hex[:6]}"
    # Pointing at another server's or a shared credential path is refused.
    for ref in ("test/platform/mcp-servers/other", "test/platform/ai-gateway/anthropic", "test/tenants/x/hubspot/y"):
        r = harness.client.post(
            "/api/v1/platform/ai/mcp-servers", json=_server_body(key=key, vault_ref=ref), headers=harness.admin()
        )
        assert r.status_code == 422, ref
    # An admin-like or platform-service identity is refused.
    for identity in ("svc-tool-execution", "mcp-admin", "root-mcp"):
        r = harness.client.post(
            "/api/v1/platform/ai/mcp-servers",
            json=_server_body(key=key, service_identity=identity),
            headers=harness.admin(),
        )
        assert r.status_code == 422, identity
    assert (
        harness.client.post(
            "/api/v1/platform/ai/mcp-servers", json=_server_body(key=key), headers=harness.admin()
        ).status_code
        == 201
    )
    # Identity and credential reference can never be shared between servers.
    dup = _server_body(key=f"s{uuid.uuid4().hex[:6]}", service_identity=f"mcp-{key}")
    assert harness.client.post("/api/v1/platform/ai/mcp-servers", json=dup, headers=harness.admin()).status_code == 409


def _create_server(h, *, pin=True):
    key = f"s{uuid.uuid4().hex[:6]}"
    body = _server_body(key=key)
    if pin:
        body["pin"] = {"server_name": "vendor-docs", "version": "1.4.2", "sha256": SHA}
    r = h.client.post("/api/v1/platform/ai/mcp-servers", json=body, headers=h.admin())
    assert r.status_code == 201, r.text
    return r.json()


def test_enabling_requires_a_reviewed_pin_and_identity_changes_reset_it(harness):
    admin = harness.admin()
    unpinned = _create_server(harness, pin=False)
    r = harness.client.post(f"/api/v1/platform/ai/mcp-servers/{unpinned['id']}/enable", headers=admin)
    assert r.status_code == 409
    server = _create_server(harness)
    assert server["pinned_version"] == "1.4.2" and server["reviewed_by"]
    r = harness.client.post(f"/api/v1/platform/ai/mcp-servers/{server['id']}/enable", headers=admin)
    assert r.status_code == 200 and r.json()["enabled"] is True
    # Changing the endpoint is a different server: the pin is cleared and the server disabled.
    r = harness.client.patch(
        f"/api/v1/platform/ai/mcp-servers/{server['id']}",
        json={"expected_version": r.json()["row_version"], "endpoint": "https://mcp.vendor.example/v2/mcp"},
        headers=admin,
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["enabled"] is False and out["pinned_version"] is None and out["reviewed_by"] is None
    # Stale writes are refused.
    stale = harness.client.patch(
        f"/api/v1/platform/ai/mcp-servers/{server['id']}",
        json={"expected_version": 1, "owner": "someone"},
        headers=admin,
    )
    assert stale.status_code == 409
    detail = harness.client.get(
        f"/api/v1/platform/ai/mcp-servers/{server['id']}", headers=harness.admin({"platform:ai:read"})
    )
    changes = [v["change"] for v in detail.json()["versions"]]
    assert changes[:3] == ["updated", "enabled", "created"]
    # Revocation is permanent.
    r = harness.client.post(
        f"/api/v1/platform/ai/mcp-servers/{server['id']}/revoke", json={"reason": "vendor compromised"}, headers=admin
    )
    assert r.status_code == 200 and r.json()["revoked"] is True
    assert (
        harness.client.post(f"/api/v1/platform/ai/mcp-servers/{server['id']}/enable", headers=admin).status_code == 409
    )


def test_server_history_is_append_only(harness):
    server = _create_server(harness)

    async def tamper(db):
        async with db.global_session() as s:
            try:
                await s.execute(
                    text("UPDATE mcp_server_versions SET change = 'enabled' WHERE server_id = :i"), {"i": server["id"]}
                )
            except Exception as exc:  # noqa: BLE001
                return type(exc).__name__
        return "updated"

    assert harness.sql(tamper) != "updated"


def _mcp_tool(h, server, suffix="search", **over):
    prefix = f"mcp.{server['key'].replace('-', '_')}."
    body = {
        "tool_id": prefix + suffix,
        "server_id": server["id"],
        "tool_name": "search_docs",
        "display_name": "Search vendor docs",
        "description": "Search the vendor documentation",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "maxLength": 300}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "risk_class": 0,
        "side_effecting": False,
    }
    body.update(over)
    return h.client.post("/api/v1/platform/ai/tools", json=body, headers=h.admin())


def test_mcp_tools_are_namespaced_schema_checked_and_gated_by_server(harness):
    server = _create_server(harness)
    assert _mcp_tool(harness, server, tool_id="crm.add_note_v2").status_code == 422  # cannot shadow internal ids
    unsafe = [
        {"type": "object", "properties": {"query": {"type": "string", "maxLength": 10}}},  # not strict
        {
            "type": "object",
            "properties": {"tenant_id": {"type": "string", "maxLength": 36}},
            "additionalProperties": False,
        },
        {"type": "object", "properties": {"approved": {"type": "boolean"}}, "additionalProperties": False},
        {"type": "object", "properties": {"q": {"type": "string"}}, "additionalProperties": False},  # unbounded
        {"type": "object", "properties": {"q": {}}, "additionalProperties": False},  # untyped
        {"type": "object", "properties": {"q": {"$ref": "#/x"}}, "additionalProperties": False},
    ]
    for schema in unsafe:
        assert _mcp_tool(harness, server, input_schema=schema).status_code == 422, schema
    r = _mcp_tool(harness, server, risk_class=3, side_effecting=False)
    assert r.status_code == 201, r.text
    tool = r.json()
    assert tool["enabled"] is False and tool["requires_approval"] is True and tool["side_effecting"] is True
    assert tool["target"] == {"tool_name": "search_docs"}
    admin = harness.admin()
    assert harness.client.post(f"/api/v1/platform/ai/tools/{tool['tool_id']}/enable", headers=admin).status_code == 200
    tenant = _tenant()
    agent = harness.add_agent(tenant)
    assert _grant(harness, tenant, agent, [tool["tool_id"]]).status_code == 200
    # Server not yet enabled: the tool is unusable.
    assert _check(harness, tenant, agent, tool["tool_id"]).json()["reason"] == "server_disabled"
    assert (
        harness.client.post(f"/api/v1/platform/ai/mcp-servers/{server['id']}/enable", headers=admin).status_code == 200
    )
    check = _check(harness, tenant, agent, tool["tool_id"]).json()
    assert check["granted"] is True and check["requires_approval"] is True
    target = check["target"]
    assert target["kind"] == "mcp" and target["tool_name"] == "search_docs"
    assert target["pinned_version"] == "1.4.2" and target["vault_ref"] == server["vault_ref"]
    assert target["allowed_hosts"] == ["mcp.vendor.example"]
    assert tool["tool_id"] in _agent_tools(harness, tenant, agent)


def test_identity_mismatch_event_auto_disables_the_server(harness):
    server = _create_server(harness)
    admin = harness.admin()
    assert (
        harness.client.post(f"/api/v1/platform/ai/mcp-servers/{server['id']}/enable", headers=admin).status_code == 200
    )
    tenant = _tenant()

    def event(producer="tool-execution", kind="mcp_server_identity_mismatch"):
        return make_event(
            "ai.security_event",
            producer=producer,
            actor=ActorRef(type="service", id="tool-execution"),
            correlation_id=uuid.uuid4(),
            tenant_id=tenant,
            payload={"kind": kind, "tool_id": "mcp.x.search", "detail": f"server_id={server['id']} reported=evil@9"},
        )

    async def deliver(db, ev):
        async with db.tenant_session(tenant, Actor("service", "mcp_registry")) as s:
            await on_security_event(ev, s)

    harness.sql(lambda db: deliver(db, event(kind="tool_denied")))
    assert _server_state(harness, server["id"])["enabled"] is True
    harness.sql(lambda db: deliver(db, event()))
    state = _server_state(harness, server["id"])
    assert state["enabled"] is False and state["health"] == "identity_mismatch"
    harness.sql(lambda db: deliver(db, event()))  # duplicate: no second history row
    detail = harness.client.get(f"/api/v1/platform/ai/mcp-servers/{server['id']}", headers=admin).json()
    assert [v["change"] for v in detail["versions"]].count("auto_disabled") == 1
    # Re-enabling needs a fresh review (re-pin) first.
    assert (
        harness.client.post(f"/api/v1/platform/ai/mcp-servers/{server['id']}/enable", headers=admin).status_code == 409
    )
    from mcp_registry.consumers import ALLOWED_PRODUCERS

    assert ALLOWED_PRODUCERS["ai.security_event"] == {"tool-execution"}


def _server_state(h, server_id):
    async def go(db):
        async with db.global_session() as s:
            return dict((await s.execute(select(mcp_servers).where(mcp_servers.c.id == server_id))).mappings().one())

    return h.sql(go)


# --------------------------------------------------------------------------- policy lifecycle
def test_policy_propose_approve_deploy_rollback(harness):
    proposer, approver = harness.admin(), harness.admin()
    current = harness.client.get("/api/v1/platform/ai/policies", headers=proposer).json()
    base = current["active_version"]
    policy = {**current["active"], "disabled_tools": ["crm.add_note"], "agent_calls_per_minute": 30}
    r = harness.client.post(
        "/api/v1/platform/ai/policies/proposals",
        json={"policy": policy, "reason": "Freeze CRM writes during audit"},
        headers=proposer,
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    # Deploying before approval, or approving your own proposal, is refused.
    assert (
        harness.client.post(f"/api/v1/platform/ai/policies/proposals/{pid}/deploy", headers=proposer).status_code == 409
    )
    assert (
        harness.client.post(
            f"/api/v1/platform/ai/policies/proposals/{pid}/approve", json={}, headers=proposer
        ).status_code
        == 403
    )
    assert (
        harness.client.post(
            f"/api/v1/platform/ai/policies/proposals/{pid}/approve", json={}, headers=harness.admin(mfa=False)
        ).status_code
        == 401
    )
    assert (
        harness.client.post(
            f"/api/v1/platform/ai/policies/proposals/{pid}/approve", json={}, headers=approver
        ).status_code
        == 200
    )
    r = harness.client.post(f"/api/v1/platform/ai/policies/proposals/{pid}/deploy", headers=proposer)
    assert r.status_code == 200, r.text
    deployed = r.json()["deployed_version"]
    assert deployed == base + 1
    tenant = _tenant()
    agent = harness.add_agent(tenant)
    assert _grant(harness, tenant, agent, ["crm.add_note"]).status_code == 200
    check = _check(harness, tenant, agent, "crm.add_note").json()
    assert check["granted"] is False and check["reason"] == "policy_disabled" and check["policy_version"] == deployed
    assert _check(harness, tenant, agent, "crm.add_note").json()["limits"]["agent_calls_per_minute"] == 30
    # Rollback restores the previous policy as a new version, with history.
    r = harness.client.post(f"/api/v1/platform/ai/policies/versions/{base}/rollback", headers=proposer)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["active_version"] == deployed + 1 and body["items"][0]["rollback_of"] == base
    assert _check(harness, tenant, agent, "crm.add_note").json()["granted"] is True


def test_policies_cannot_weaken_the_security_floor(harness):
    admin = harness.admin()
    base = harness.client.get("/api/v1/platform/ai/policies", headers=admin).json()["active"]
    for weaker in (
        {**base, "approval_required_classes": [2]},
        {**base, "max_agent_risk_class": 4},
        {**base, "mcp_require_pinned": False},
        {**base, "unknown_knob": True},
    ):
        r = harness.client.post(
            "/api/v1/platform/ai/policies/proposals",
            json={"policy": weaker, "reason": "try to weaken it"},
            headers=admin,
        )
        assert r.status_code == 422, weaker
    assert (
        harness.client.get("/api/v1/platform/ai/policies", headers=harness.user(_tenant(), MANAGE)).status_code == 403
    )


def test_stale_proposal_cannot_be_deployed(harness):
    a, b = harness.admin(), harness.admin()
    active = harness.client.get("/api/v1/platform/ai/policies", headers=a).json()["active"]
    ids = []
    for n in (10, 20):
        r = harness.client.post(
            "/api/v1/platform/ai/policies/proposals",
            json={"policy": {**active, "agent_calls_per_minute": n}, "reason": "rate tuning"},
            headers=a,
        )
        ids.append(r.json()["id"])
        assert (
            harness.client.post(
                f"/api/v1/platform/ai/policies/proposals/{ids[-1]}/approve", json={}, headers=b
            ).status_code
            == 200
        )
    assert harness.client.post(f"/api/v1/platform/ai/policies/proposals/{ids[0]}/deploy", headers=a).status_code == 200
    assert harness.client.post(f"/api/v1/platform/ai/policies/proposals/{ids[1]}/deploy", headers=a).status_code == 409


def test_seed_is_idempotent(harness):
    from mcp_registry.seed import seed
    from nexus_core.devseed import ACME, demo_id

    async def go(db):
        await seed(db)
        await seed(db)
        async with db.tenant_session(ACME.id) as s:
            return (
                (
                    await s.execute(
                        text("SELECT tool_id FROM agent_tool_grants WHERE agent_id = :a"), {"a": demo_id("agent", 1)}
                    )
                )
                .scalars()
                .all()
            )

    assert len(harness.sql(go)) == 7

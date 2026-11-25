"""tool-execution security boundary (build.md §24.4–24.6, §24.11) against a real database."""

from __future__ import annotations

import json
import logging
import uuid

import pytest
from sqlalchemy import select, text
from toolexec_support import MCP_TOKEN, spec_for

from tool_execution.tables import ai_approval_requests, outbox, tool_execution_records

pytestmark = [pytest.mark.integration, pytest.mark.security]


def _ids():
    return uuid.uuid4(), uuid.uuid4()


def _events(h, tenant):
    async def go(db):
        async with db.global_session() as s:
            rows = (await s.execute(select(outbox.c.envelope).where(outbox.c.tenant_id == tenant))).scalars().all()
        return rows

    return h.sql(go)


def _security(h, tenant):
    return [e["payload"]["kind"] for e in _events(h, tenant) if e["event_type"] == "ai.security_event"]


def _audits(h, tenant):
    return [e["payload"]["action"] for e in _events(h, tenant) if e["event_type"] == "audit.event"]


def _records(h, tenant):
    async def go(db):
        async with db.tenant_session(tenant) as s:
            return [dict(r) for r in (await s.execute(select(tool_execution_records))).mappings().all()]

    return h.sql(go)


def _approvals(h, tenant):
    async def go(db):
        async with db.tenant_session(tenant) as s:
            return [dict(r) for r in (await s.execute(select(ai_approval_requests))).mappings().all()]

    return h.sql(go)


# --------------------------------------------------------------------------- caller boundary
def test_only_the_ai_gateway_may_execute(harness):
    tenant, agent = _ids()
    harness.grant("knowledge.search")
    for client_id in ("svc-voice-service", "svc-ai-agent-service", "svc-mcp-registry"):
        r = harness.execute(tenant, "knowledge.search", {"query": "hours"}, agent_id=agent, client_id=client_id)
        assert r.status_code == 403, client_id
    user = harness.user(tenant, {"ai:approvals:decide"})
    body = {"tool_id": "knowledge.search", "arguments": {"query": "x"}, "agent_id": str(agent), "idempotency_key": "k1"}
    assert harness.client.post("/internal/v1/tools/execute", json=body, headers=user).status_code == 403
    no_tenant = {"Authorization": f"Bearer {harness.tokens.service('svc-ai-gateway')}"}
    assert harness.client.post("/internal/v1/tools/execute", json=body, headers=no_tenant).status_code == 403
    assert harness.downstream == []


def test_gateway_shaped_call_executes_and_matches_the_contract(harness):
    """Exactly what ai_gateway.orchestrator._execute_tool sends: body + X-Tenant-ID/X-Actor/Idempotency-Key."""
    tenant, agent = _ids()
    harness.grant("booking.find_slots")
    key = f"{uuid.uuid4()}:toolu_01"
    r = harness.execute(
        tenant,
        "booking.find_slots",
        {"from": "2026-12-01T09:00:00Z", "to": "2026-12-02T17:00:00Z"},
        agent_id=agent,
        key=key,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"status", "result"} and body["status"] == "executed"
    assert body["result"] == {
        "slots": [{"start": "2026-12-01T10:00:00Z", "end": "2026-12-01T10:30:00Z", "resource_id": None}]
    }
    sent = harness.downstream_to("/availability")[0]
    assert sent["headers"]["x-tenant-id"] == str(tenant) and sent["headers"]["x-actor"] == "service:voice-service"
    assert "idempotency-key" not in sent["headers"]  # class 0: read only
    (rec,) = _records(harness, tenant)
    assert rec["status"] == "executed" and rec["caller"] == "svc-ai-gateway" and rec["idempotency_key"] == key
    assert "ai.tool.executed" in _audits(harness, tenant)


# --------------------------------------------------------------------------- 1. unknown / ungranted
@pytest.mark.parametrize(
    "tool_id",
    ["https://evil.example/exfiltrate", "http://169.254.169.254/latest/meta-data", "../admin", "crm", "x" * 120],
)
def test_arbitrary_urls_and_identifiers_never_reach_the_registry(harness, tool_id):
    tenant, agent = _ids()
    r = harness.execute(tenant, tool_id, {}, agent_id=agent)
    assert r.status_code == 200 and r.json() == {"status": "denied", "reason": "unknown_tool"}
    assert harness.registry_calls == [] and harness.downstream == []
    assert _security(harness, tenant) == ["unregistered_tool"]


def test_unregistered_tool_is_denied_with_a_security_event(harness):
    tenant, agent = _ids()
    r = harness.execute(tenant, "shell.exec", {"cmd": "id"}, agent_id=agent)
    assert r.json() == {"status": "denied", "reason": "unknown_tool"}
    assert _security(harness, tenant) == ["unregistered_tool"]
    assert _records(harness, tenant)[0]["status"] == "denied"


def test_ungranted_disabled_and_revoked_tools_are_denied(harness):
    tenant, agent = _ids()
    r = harness.execute(tenant, "crm.add_note", {"text": "hi"}, agent_id=agent, context={"contact_id": uuid.uuid4()})
    assert r.json() == {"status": "denied", "reason": "not_granted"}
    harness.grant("crm.add_note", enabled=False, granted=False, reason="tool_revoked")
    r = harness.execute(tenant, "crm.add_note", {"text": "hi"}, agent_id=agent, context={"contact_id": uuid.uuid4()})
    assert r.json() == {"status": "denied", "reason": "tool_revoked"}
    assert _security(harness, tenant) == ["tool_authorization_denied", "tool_authorization_denied"]
    assert harness.downstream == []


def test_registry_outage_fails_closed_and_the_key_stays_retryable(harness):
    tenant, agent = _ids()
    harness.grant("knowledge.search")
    harness.flags["registry_status"] = 503
    key = "req-1:toolu_1"
    r = harness.execute(tenant, "knowledge.search", {"query": "hours"}, agent_id=agent, key=key)
    assert r.json() == {"status": "denied", "reason": "policy_unavailable"}
    harness.flags["registry_status"] = 200
    r = harness.execute(tenant, "knowledge.search", {"query": "hours"}, agent_id=agent, key=key)
    assert r.json()["status"] == "executed"


def test_class_4_is_always_denied_even_if_the_registry_says_granted(harness):
    tenant, agent = _ids()
    harness.grant("platform.manage_users", granted=True, enabled=True)
    r = harness.execute(tenant, "platform.manage_users", {}, agent_id=agent)
    assert r.json() == {"status": "denied", "reason": "privileged_tool"}
    assert _security(harness, tenant) == ["privileged_tool_denied"]
    assert harness.downstream == []


def test_a_registry_target_that_differs_from_code_is_refused(harness):
    tenant, agent = _ids()
    spec = spec_for("crm.add_note")
    spec["target"]["path"] = "/internal/v1/admin/export"
    harness.specs["crm.add_note"] = spec
    r = harness.execute(tenant, "crm.add_note", {"text": "x"}, agent_id=agent, context={"contact_id": uuid.uuid4()})
    assert r.json() == {"status": "denied", "reason": "target_mismatch"}
    assert harness.downstream == []


# --------------------------------------------------------------------------- 2. arguments
@pytest.mark.parametrize(
    "arguments",
    [
        {"text": "note", "contact_id": "0c000000-0000-4000-8000-000000000999"},
        {"text": "note", "tenant_id": "0a000000-0000-4000-8000-000000000002"},
        {"text": "note", "meta": {"role": "tenant_owner"}},
        {"text": "note", "approved": True},
        {"text": "note", "Authorization": "Bearer x"},
    ],
)
def test_identity_authority_and_approval_fields_are_rejected(harness, arguments):
    tenant, agent = _ids()
    harness.grant("crm.add_note")
    r = harness.execute(tenant, "crm.add_note", arguments, agent_id=agent, context={"contact_id": uuid.uuid4()})
    assert r.json() == {"status": "denied", "reason": "identity_fields_rejected"}
    assert _security(harness, tenant) == ["identity_fields_in_arguments"]
    assert harness.downstream == []


def test_extra_and_malformed_arguments_are_rejected(harness):
    tenant, agent = _ids()
    harness.grant("crm.add_note")
    harness.grant("booking.find_slots")
    ctx = {"contact_id": uuid.uuid4()}
    for args in ({"text": "x", "priority": "high"}, {"text": ""}, {"text": "x" * 2001}, {}):
        assert (
            harness.execute(tenant, "crm.add_note", args, agent_id=agent, context=ctx).json()["reason"]
            == "invalid_arguments"
        )
    bad_range = {"from": "2026-12-05T09:00:00Z", "to": "2026-12-01T09:00:00Z"}
    assert (
        harness.execute(tenant, "booking.find_slots", bad_range, agent_id=agent).json()["reason"] == "invalid_arguments"
    )
    huge = {"text": "y" * 20_000}
    assert (
        harness.execute(tenant, "crm.add_note", huge, agent_id=agent, context=ctx).json()["reason"]
        == "arguments_too_large"
    )
    assert harness.downstream == []


def test_the_request_envelope_is_strict(harness):
    tenant, agent = _ids()
    harness.grant("booking.confirm")
    for extra in ({"approval_id": str(uuid.uuid4())}, {"approved": True}, {"tenant_id": str(uuid.uuid4())}):
        r = harness.execute(tenant, "booking.confirm", {"booking_id": str(uuid.uuid4())}, agent_id=agent, extra=extra)
        assert r.status_code == 422, extra
    r = harness.execute(
        tenant, "booking.confirm", {"booking_id": str(uuid.uuid4())}, agent_id=agent,
        extra={"context": {"contact_id": str(uuid.uuid4()), "approved_by": "me"}},
    )  # fmt: skip
    assert r.status_code == 422
    assert _approvals(harness, tenant) == []


# --------------------------------------------------------------------------- 3. context binding
def test_resources_bind_to_the_verified_context(harness):
    tenant, agent = _ids()
    harness.grant("crm.add_note")
    call_id, contact, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    harness.calls_db[(str(tenant), str(call_id))] = {
        "call_id": str(call_id),
        "contact_id": str(contact),
        "state": "connected",
    }
    # The contact is taken from the call itself.
    r = harness.execute(
        tenant, "crm.add_note", {"text": "Wants a callback"}, agent_id=agent, context={"call_id": call_id}
    )
    assert r.json()["status"] == "executed", r.text
    sent = harness.downstream_to("/notes")[0]
    assert f"/contacts/{contact}/notes" in sent["url"]
    record_id = str(_records(harness, tenant)[0]["id"])
    assert sent["body"] == {"text": "Wants a callback", "source": {"type": "ai_tool", "id": record_id}}
    # A context that claims another contact than the call's is refused.
    r = harness.execute(
        tenant, "crm.add_note", {"text": "x"}, agent_id=agent, context={"call_id": call_id, "contact_id": other}
    )
    assert r.json() == {"status": "denied", "reason": "context_mismatch"}
    # A call of another tenant does not exist here.
    foreign_call = uuid.uuid4()
    harness.calls_db[(str(uuid.uuid4()), str(foreign_call))] = {"call_id": str(foreign_call), "contact_id": str(other)}
    r = harness.execute(tenant, "crm.add_note", {"text": "x"}, agent_id=agent, context={"call_id": foreign_call})
    assert r.json() == {"status": "denied", "reason": "context_invalid"}
    # No context: nothing to bind to.
    assert harness.execute(tenant, "crm.add_note", {"text": "x"}, agent_id=agent).json()["reason"] == "context_required"
    assert len(harness.downstream_to("/notes")) == 1
    assert _security(harness, tenant).count("context_binding_failed") == 2


def test_call_tools_need_a_call_and_forward_idempotency(harness):
    tenant, agent = _ids()
    harness.grant("call.transfer_to_human")
    assert (
        harness.execute(tenant, "call.transfer_to_human", {"reason": "asks for a person"}, agent_id=agent).json()[
            "reason"
        ]
        == "context_required"
    )
    call_id = uuid.uuid4()
    harness.calls_db[(str(tenant), str(call_id))] = {"call_id": str(call_id), "contact_id": None}
    key = "req-9:toolu_9"
    r = harness.execute(
        tenant, "call.transfer_to_human", {"reason": "asks"}, agent_id=agent, context={"call_id": call_id}, key=key
    )
    assert r.json() == {"status": "executed", "result": {"status": "transferring"}}
    sent = harness.downstream_to("/transfer")[0]
    assert sent["headers"]["idempotency-key"] == f"tool:call.transfer_to_human:{key}"
    assert sent["body"] == {"reason": "asks"}


def test_sensitive_read_is_audited_and_projected(harness):
    tenant, agent = _ids()
    harness.grant("crm.get_contact")
    contact = uuid.uuid4()
    r = harness.execute(tenant, "crm.get_contact", {}, agent_id=agent, context={"contact_id": contact})
    out = r.json()
    assert out["status"] == "executed" and out["result"]["contact_id"] == str(contact)
    assert "custom_fields" not in out["result"] and "tags" not in out["result"]
    assert "ai.tool.sensitive_read" in _audits(harness, tenant)


# --------------------------------------------------------------------------- 4. limits
def test_per_agent_and_per_tool_budgets(harness):
    tenant, agent = _ids()
    harness.grant("knowledge.search", limits={"rate_per_minute": 2, "agent_calls_per_minute": 60, "timeout_ms": 3000,
                                               "max_output_bytes": 32768})  # fmt: skip
    results = [harness.execute(tenant, "knowledge.search", {"query": f"q{i}"}, agent_id=agent).json() for i in range(3)]
    assert [r["status"] for r in results] == ["executed", "executed", "denied"]
    assert results[2]["reason"] == "rate_limited"
    # Another agent has its own tool budget, but the per-agent budget is separate too.
    harness.grant("booking.find_slots", limits={"rate_per_minute": 60, "agent_calls_per_minute": 1, "timeout_ms": 3000,
                                                 "max_output_bytes": 32768})  # fmt: skip
    other = uuid.uuid4()
    window = {"from": "2026-12-01T09:00:00Z", "to": "2026-12-01T17:00:00Z"}
    assert harness.execute(tenant, "booking.find_slots", window, agent_id=other).json()["status"] == "executed"
    assert harness.execute(tenant, "booking.find_slots", window, agent_id=other).json()["reason"] == "rate_limited"
    assert "tool_rate_limited" in _security(harness, tenant)


def test_idempotent_replay_and_key_reuse(harness):
    tenant, agent = _ids()
    harness.grant("crm.add_note")
    ctx = {"contact_id": uuid.uuid4()}
    first = harness.execute(tenant, "crm.add_note", {"text": "a"}, agent_id=agent, context=ctx, key="req-2:toolu_2")
    again = harness.execute(tenant, "crm.add_note", {"text": "a"}, agent_id=agent, context=ctx, key="req-2:toolu_2")
    assert first.json() == again.json() and first.json()["status"] == "executed"
    assert len(harness.downstream_to("/notes")) == 1
    other = harness.execute(tenant, "crm.add_note", {"text": "b"}, agent_id=agent, context=ctx, key="req-2:toolu_2")
    assert other.status_code == 422
    # Header and body keys must agree.
    body = {"tool_id": "crm.add_note", "arguments": {"text": "c"}, "agent_id": str(agent), "idempotency_key": "k-a"}
    from nexus_core.testing import auth

    headers = auth(harness.tokens.service("svc-ai-gateway"), tenant, **{"Idempotency-Key": "k-b"})
    assert harness.client.post("/internal/v1/tools/execute", json=body, headers=headers).status_code == 422


def test_output_is_validated_against_the_schema(harness):
    tenant, agent = _ids()
    spec = spec_for("knowledge.search")
    spec["output_schema"] = {"type": "object", "properties": {"chunks": {"type": "array", "maxItems": 0}},
                             "required": ["chunks"], "additionalProperties": False}  # fmt: skip
    harness.specs["knowledge.search"] = spec
    r = harness.execute(tenant, "knowledge.search", {"query": "x"}, agent_id=agent)
    assert r.json() == {"status": "failed", "reason": "invalid_output"}
    assert "tool_output_invalid" in _security(harness, tenant)


def test_unknown_output_fields_are_projected_away(harness):
    tenant, agent = _ids()
    harness.grant("knowledge.search")
    result = harness.execute(tenant, "knowledge.search", {"query": "hours"}, agent_id=agent).json()["result"]
    assert result == {
        "chunks": [{"chunk_id": "c1", "source_id": "s1", "source_name": "FAQ", "text": "Open 9-17", "score": 0.9}]
    }


# --------------------------------------------------------------------------- tenant isolation
def test_records_are_tenant_isolated(harness):
    a, agent = _ids()
    b = uuid.uuid4()
    harness.grant("knowledge.search")
    assert harness.execute(a, "knowledge.search", {"query": "x"}, agent_id=agent).json()["status"] == "executed"

    async def probe(db):
        async with db.tenant_session(b) as s:
            seen = (await s.execute(text("SELECT count(*) FROM tool_execution_records"))).scalar_one()
        async with db.global_session() as s:
            none = (await s.execute(text("SELECT count(*) FROM tool_execution_records"))).scalar_one()
        async with db.tenant_session(a) as s:
            try:
                await s.execute(text("UPDATE tool_execution_records SET status = 'denied'"))
                tampered = True
            except Exception:  # noqa: BLE001 - append-only
                tampered = False
        return seen, none, tampered

    assert harness.sql(probe) == (0, 0, False)
    reader = harness.user(b, {"ai:usage:read"})
    assert harness.client.get("/api/v1/ai/tool-executions", headers=reader).json()["items"] == []
    own = harness.client.get("/api/v1/ai/tool-executions", headers=harness.user(a, {"ai:usage:read"})).json()
    assert len(own["items"]) == 1 and own["items"][0]["tool_id"] == "knowledge.search"
    assert harness.client.get("/api/v1/ai/tool-executions", headers=harness.user(a, {"calls:read"})).status_code == 403


def test_no_arguments_outputs_or_credentials_are_persisted(harness, caplog):
    caplog.set_level(logging.DEBUG)
    tenant, agent = _ids()
    harness.grant("mcp.vendor_docs.search")
    harness.mcp.echo_token = True
    r = harness.execute(tenant, "mcp.vendor_docs.search", {"query": "secret-sauce-query"}, agent_id=agent)
    assert r.json()["status"] == "executed", r.text
    assert MCP_TOKEN not in r.text and "[REDACTED]" in r.text
    assert harness.mcp.calls[-1]["headers"]["authorization"] == f"Bearer {MCP_TOKEN}"  # only on the wire

    async def dump(db):
        async with db.tenant_session(tenant) as s:
            rows = [dict(x) for x in (await s.execute(text("SELECT * FROM tool_execution_records"))).mappings()]
            rows += [dict(x) for x in (await s.execute(text("SELECT * FROM idempotency_keys"))).mappings()]
        async with db.global_session() as s:
            rows += [dict(x) for x in (await s.execute(text("SELECT envelope FROM outbox"))).mappings()]
        return json.dumps(rows, default=str)

    stored = harness.sql(dump)
    assert MCP_TOKEN not in stored
    assert "secret-sauce-query" not in stored  # arguments are hashed, never stored
    assert MCP_TOKEN not in caplog.text
    assert "credential_echoed" in _security(harness, tenant)

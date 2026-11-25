"""Shared test helpers for tool_execution (unique module name; test dirs are not packages)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

SERVICE_DIR = Path(__file__).resolve().parents[1]
MCP_TOKEN = "mcp-cred-7f3a9c1e5b2d4086a1c3e5f7091b2d4f"  # test-only credential value
SERVER_ID = "5e000000-0000-4000-8000-000000000001"
VAULT_REF = "test/platform/mcp-servers/vendor-docs"
FAKE_DNS = {
    "mcp.vendor.example": "93.184.216.34",
    "rebind.vendor.example": "10.0.0.7",
    "metadata.vendor.example": "169.254.169.254",
}
ISO = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})$"
UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
S = {"type": "string", "maxLength": 200}
NS = {"type": ["string", "null"], "maxLength": 200}


def obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        out["required"] = required
    return out


# Mirrors the registry catalogue contract (BUILD_CONVENTIONS §5).
TOOLS: dict[str, dict[str, Any]] = {
    "knowledge.search": {
        "risk_class": 0, "side_effecting": False,
        "target": {"kind": "internal", "service": "ai-agent-service", "method": "POST", "path": "/internal/v1/knowledge/search"},
        "input_schema": obj({"query": {"type": "string", "minLength": 1, "maxLength": 500}}, ["query"]),
        "output_schema": obj({"chunks": {"type": "array", "maxItems": 8, "items": obj(
            {"chunk_id": S, "source_id": S, "source_name": NS, "text": {"type": "string", "maxLength": 4000},
             "score": {"type": ["number", "null"]}}, ["text"])}}, ["chunks"]),
    },
    "booking.find_slots": {
        "risk_class": 0, "side_effecting": False,
        "target": {"kind": "internal", "service": "booking-service", "method": "GET", "path": "/internal/v1/availability"},
        "input_schema": obj({"from": {"type": "string", "pattern": ISO}, "to": {"type": "string", "pattern": ISO}}, ["from", "to"]),
        "output_schema": obj({"slots": {"type": "array", "maxItems": 50, "items": obj(
            {"start": S, "end": S, "resource_id": NS}, ["start", "end"])}}, ["slots"]),
    },
    "crm.get_contact": {
        "risk_class": 1, "side_effecting": False,
        "target": {"kind": "internal", "service": "crm-service", "method": "GET", "path": "/internal/v1/contacts/{contact_id}"},
        "input_schema": obj({}),
        "output_schema": obj({k: NS for k in ("first_name", "last_name", "company", "timezone", "locale", "status",
                                               "email_masked", "phone_masked")} | {"contact_id": S}, ["contact_id"]),
    },
    "crm.add_note": {
        "risk_class": 2, "side_effecting": True,
        "target": {"kind": "internal", "service": "crm-service", "method": "POST", "path": "/internal/v1/contacts/{contact_id}/notes"},
        "input_schema": obj({"text": {"type": "string", "minLength": 1, "maxLength": 2000}}, ["text"]),
        "output_schema": obj({"note_id": S}, ["note_id"]),
    },
    "call.transfer_to_human": {
        "risk_class": 2, "side_effecting": True,
        "target": {"kind": "internal", "service": "call-service", "method": "POST", "path": "/internal/v1/calls/{call_id}/transfer"},
        "input_schema": obj({"reason": {"type": "string", "minLength": 1, "maxLength": 500}}, ["reason"]),
        "output_schema": obj({"status": {"type": "string", "enum": ["transferring", "unavailable"]}}, ["status"]),
    },
    "booking.confirm": {
        "risk_class": 3, "side_effecting": True, "requires_approval": True,
        "target": {"kind": "internal", "service": "booking-service", "method": "POST", "path": "/internal/v1/bookings/{booking_id}/confirm"},
        "input_schema": obj({"booking_id": {"type": "string", "pattern": UUID_RE}}, ["booking_id"]),
        "output_schema": obj({"booking_id": S, "status": S}, ["booking_id", "status"]),
    },
    "platform.manage_users": {
        "risk_class": 4, "side_effecting": True, "requires_approval": True,
        "target": {"kind": "internal", "service": "tenant-service", "method": "POST", "path": "/internal/v1/x"},
        "input_schema": obj({}), "output_schema": obj({}),
    },
    "mcp.vendor_docs.search": {
        "risk_class": 0, "side_effecting": False,
        "target": {
            "kind": "mcp", "tool_name": "search_docs", "server_id": SERVER_ID, "server_key": "vendor-docs",
            "endpoint": "https://mcp.vendor.example/mcp", "transport": "streamable_http",
            "allowed_hosts": ["mcp.vendor.example"], "trust_level": "verified", "service_identity": "mcp-vendor-docs",
            "vault_ref": VAULT_REF, "pinned_server_name": "vendor-docs", "pinned_version": "1.4.2",
            "timeout_ms": 2000, "max_concurrency": 2, "server_rate_per_minute": 100, "max_request_bytes": 16384,
            "max_response_bytes": 8192,
        },
        "input_schema": obj({"query": {"type": "string", "maxLength": 300}}, ["query"]),
        "output_schema": obj({"content": {"type": "string", "maxLength": 65536}, "structured": {"type": ["object", "null"]},
                              "is_error": {"type": "boolean"}}, ["content", "is_error"]),
    },
}  # fmt: skip


def spec_for(tool_id: str, **over: Any) -> dict[str, Any]:
    base = copy.deepcopy(TOOLS[tool_id])
    risk = base["risk_class"]
    spec = {
        "tool_id": tool_id,
        "granted": True,
        "enabled": True,
        "reason": None,
        "risk_class": risk,
        "requires_approval": base.pop("requires_approval", False) or risk >= 3,
        "side_effecting": base["side_effecting"],
        "version": 1,
        "input_schema": base["input_schema"],
        "output_schema": base["output_schema"],
        "target": base["target"],
        "limits": {"rate_per_minute": 60, "agent_calls_per_minute": 60, "timeout_ms": 3000, "max_output_bytes": 32768},
        "policy_version": 1,
    }
    spec.update(over)
    return spec

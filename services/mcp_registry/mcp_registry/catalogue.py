"""Code-defined internal tool catalogue (BUILD_CONVENTIONS §5) and JSON Schema rules for every tool.

Every schema is strict (`additionalProperties: false` at every object level) and contains no identity or
authority fields: tenant, actor, contact, call, thread, approval and credential values come from verified
server-side context, never from model-produced arguments (build.md §24.4, owasp LLM03).
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

# Property names that may never appear in a tool's input schema (at any depth).
FORBIDDEN_PROPERTIES = frozenset(
    {
        "tenant",
        "tenant_id",
        "user",
        "user_id",
        "actor",
        "actor_id",
        "agent_id",
        "role",
        "roles",
        "permission",
        "permissions",
        "scope",
        "scopes",
        "contact_id",
        "call_id",
        "thread_id",
        "approved",
        "approval",
        "approval_id",
        "approved_by",
        "credential",
        "credentials",
        "token",
        "access_token",
        "api_key",
        "secret",
        "password",
        "authorization",
        "headers",
        "url",
        "endpoint",
        "vault_ref",
    }
)

ISO_DATETIME = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})$"
UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

_STR = {"type": "string", "maxLength": 200}
_NSTR = {"type": ["string", "null"], "maxLength": 200}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


SLOT = _obj(
    {
        "start": {"type": "string", "pattern": ISO_DATETIME},
        "end": {"type": "string", "pattern": ISO_DATETIME},
        "resource_id": {"type": ["string", "null"], "pattern": UUID_PATTERN},
    },
    ["start", "end"],
)

INTERNAL_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "tool_id": "knowledge.search",
        "display_name": "Search knowledge",
        "description": "Search the tenant's approved knowledge sources. Returns short text passages.",
        "risk_class": 0,
        "side_effecting": False,
        "requires_approval": False,
        "data_classification": "internal",
        "rate_per_minute": 120,
        "target": {
            "kind": "internal",
            "service": "ai-agent-service",
            "method": "POST",
            "path": "/internal/v1/knowledge/search",
        },
        "input_schema": _obj({"query": {"type": "string", "minLength": 1, "maxLength": 500}}, ["query"]),
        "output_schema": _obj(
            {
                "chunks": {
                    "type": "array",
                    "maxItems": 8,
                    "items": _obj(
                        {
                            "chunk_id": _STR,
                            "source_id": _STR,
                            "source_name": _NSTR,
                            "text": {"type": "string", "maxLength": 4000},
                            "score": {"type": ["number", "null"]},
                        },
                        ["text"],
                    ),
                }
            },
            ["chunks"],
        ),
    },
    {
        "tool_id": "booking.find_slots",
        "display_name": "Find free slots",
        "description": "List free appointment slots between two ISO-8601 timestamps (at most 31 days apart).",
        "risk_class": 0,
        "side_effecting": False,
        "requires_approval": False,
        "data_classification": "internal",
        "rate_per_minute": 60,
        "target": {
            "kind": "internal",
            "service": "booking-service",
            "method": "GET",
            "path": "/internal/v1/availability",
        },
        "input_schema": _obj(
            {"from": {"type": "string", "pattern": ISO_DATETIME}, "to": {"type": "string", "pattern": ISO_DATETIME}},
            ["from", "to"],
        ),
        "output_schema": _obj({"slots": {"type": "array", "maxItems": 50, "items": SLOT}}, ["slots"]),
    },
    {
        "tool_id": "crm.get_contact",
        "display_name": "Read current contact",
        "description": "Read the profile of the contact this conversation is about (masked email and phone).",
        "risk_class": 1,
        "side_effecting": False,
        "requires_approval": False,
        "data_classification": "confidential",
        "rate_per_minute": 30,
        "target": {
            "kind": "internal",
            "service": "crm-service",
            "method": "GET",
            "path": "/internal/v1/contacts/{contact_id}",
        },
        "input_schema": _obj({}),
        "output_schema": _obj(
            {
                "contact_id": _STR,
                "first_name": _NSTR,
                "last_name": _NSTR,
                "company": _NSTR,
                "timezone": _NSTR,
                "locale": _NSTR,
                "status": _NSTR,
                "email_masked": _NSTR,
                "phone_masked": _NSTR,
            },
            ["contact_id"],
        ),
    },
    {
        "tool_id": "crm.add_note",
        "display_name": "Add a note to the contact",
        "description": "Append a short note to the current contact's record.",
        "risk_class": 2,
        "side_effecting": True,
        "requires_approval": False,
        "data_classification": "confidential",
        "rate_per_minute": 20,
        "target": {
            "kind": "internal",
            "service": "crm-service",
            "method": "POST",
            "path": "/internal/v1/contacts/{contact_id}/notes",
        },
        "input_schema": _obj({"text": {"type": "string", "minLength": 1, "maxLength": 2000}}, ["text"]),
        "output_schema": _obj({"note_id": _STR}, ["note_id"]),
    },
    {
        "tool_id": "booking.create_tentative",
        "display_name": "Hold a slot (tentative booking)",
        "description": "Create a tentative booking for the current contact. It expires unless confirmed.",
        "risk_class": 2,
        "side_effecting": True,
        "requires_approval": False,
        "data_classification": "internal",
        "rate_per_minute": 10,
        "target": {
            "kind": "internal",
            "service": "booking-service",
            "method": "POST",
            "path": "/internal/v1/bookings/tentative",
        },
        "input_schema": _obj(
            {"start": {"type": "string", "pattern": ISO_DATETIME}, "end": {"type": "string", "pattern": ISO_DATETIME}},
            ["start", "end"],
        ),
        "output_schema": _obj(
            {"booking_id": _STR, "status": _STR, "start": _NSTR, "end": _NSTR}, ["booking_id", "status"]
        ),
    },
    {
        "tool_id": "call.transfer_to_human",
        "display_name": "Transfer the call to a person",
        "description": "Hand the current call over to a human agent.",
        "risk_class": 2,
        "side_effecting": True,
        "requires_approval": False,
        "data_classification": "internal",
        "rate_per_minute": 5,
        "target": {
            "kind": "internal",
            "service": "call-service",
            "method": "POST",
            "path": "/internal/v1/calls/{call_id}/transfer",
        },
        "input_schema": _obj({"reason": {"type": "string", "minLength": 1, "maxLength": 500}}, ["reason"]),
        "output_schema": _obj({"status": {"type": "string", "enum": ["transferring", "unavailable"]}}, ["status"]),
    },
    {
        "tool_id": "booking.confirm",
        "display_name": "Confirm a booking",
        "description": "Confirm a tentative booking. Customer-facing: always needs a person's approval.",
        "risk_class": 3,
        "side_effecting": True,
        "requires_approval": True,
        "data_classification": "internal",
        "rate_per_minute": 10,
        "target": {
            "kind": "internal",
            "service": "booking-service",
            "method": "POST",
            "path": "/internal/v1/bookings/{booking_id}/confirm",
        },
        "input_schema": _obj({"booking_id": {"type": "string", "pattern": UUID_PATTERN}}, ["booking_id"]),
        "output_schema": _obj({"booking_id": _STR, "status": _STR}, ["booking_id", "status"]),
    },
)

# Default output envelope for MCP tools (tool-execution shapes every MCP result like this).
MCP_DEFAULT_OUTPUT_SCHEMA = _obj(
    {
        "content": {"type": "string", "maxLength": 65536},
        "structured": {"type": ["object", "null"]},
        "is_error": {"type": "boolean"},
    },
    ["content", "is_error"],
)


class UnsafeSchema(ValueError):
    pass


def _walk_object(schema: dict[str, Any], path: str, *, input_side: bool) -> None:
    # Inputs are strict at every level. Outputs are strict at the top level; nested structured data from
    # an external server may be open (it is size-capped and always handed to the model as untrusted data).
    if schema.get("additionalProperties") is not False and (input_side or path == "$"):
        raise UnsafeSchema(f"{path}: objects must set additionalProperties: false")
    if "patternProperties" in schema:
        raise UnsafeSchema(f"{path}: patternProperties is not allowed")
    for name, sub in (schema.get("properties") or {}).items():
        if input_side and name.lower() in FORBIDDEN_PROPERTIES:
            raise UnsafeSchema(f"{path}.{name}: identity, authority and credential fields are not allowed")
        _walk(sub, f"{path}.{name}", input_side=input_side)


def _walk(schema: Any, path: str, *, input_side: bool) -> None:
    if not isinstance(schema, dict):
        return
    stype = schema.get("type")
    types = set(stype) if isinstance(stype, list) else {stype}
    constrained = any(k in schema for k in ("enum", "const", "anyOf", "oneOf", "allOf"))
    if input_side and stype is None and not constrained:
        raise UnsafeSchema(f"{path}: every input value must declare a type")
    if "object" in types:
        _walk_object(schema, path, input_side=input_side)
    if "array" in types:
        if "maxItems" not in schema:
            raise UnsafeSchema(f"{path}: arrays must declare maxItems")
        _walk(schema.get("items"), f"{path}[]", input_side=input_side)
    bounded = any(k in schema for k in ("maxLength", "enum", "pattern", "const"))
    if "string" in types and input_side and not bounded:
        raise UnsafeSchema(f"{path}: strings must declare maxLength, pattern or enum")
    for key in ("anyOf", "oneOf", "allOf"):
        for i, sub in enumerate(schema.get(key) or []):
            _walk(sub, f"{path}.{key}[{i}]", input_side=input_side)
    if "$ref" in schema:
        raise UnsafeSchema(f"{path}: $ref is not allowed (schemas must be self-contained)")


def check_tool_schema(schema: dict[str, Any], *, input_side: bool) -> None:
    """Raise UnsafeSchema unless `schema` is a valid, strict, self-contained JSON Schema object."""
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise UnsafeSchema(f"invalid JSON Schema: {exc.message[:200]}") from exc
    if schema.get("type") != "object":
        raise UnsafeSchema("the top-level schema must be an object")
    _walk(schema, "$", input_side=input_side)


for _tool in INTERNAL_TOOLS:  # the seed catalogue must satisfy its own rules
    check_tool_schema(_tool["input_schema"], input_side=True)
    check_tool_schema(_tool["output_schema"], input_side=False)
check_tool_schema(MCP_DEFAULT_OUTPUT_SCHEMA, input_side=False)

"""Shared test helpers for ai_gateway (unique module name; test dirs are not packages)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

SERVICE_DIR = Path(__file__).resolve().parents[1]
TENANT_A = uuid.UUID("0a000000-0000-4000-8000-0000000000aa")
TENANT_B = uuid.UUID("0a000000-0000-4000-8000-0000000000bb")
AGENT = uuid.UUID("0b000000-0000-4000-8000-000000000001")
CONTACT = uuid.UUID("0c000000-0000-4000-8000-000000000001")

TOOLS = {
    "knowledge.search": {
        "risk_class": 0,
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "maxLength": 500}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "booking.find_slots": {
        "risk_class": 0,
        "input_schema": {
            "type": "object",
            "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
            "required": ["from", "to"],
            "additionalProperties": False,
        },
    },
    "booking.create_tentative": {
        "risk_class": 2,
        "input_schema": {
            "type": "object",
            "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
            "required": ["start", "end"],
            "additionalProperties": False,
        },
    },
    "booking.confirm": {
        "risk_class": 3,
        "input_schema": {
            "type": "object",
            "properties": {"booking_id": {"type": "string"}},
            "required": ["booking_id"],
            "additionalProperties": False,
        },
    },
    "platform.manage_users": {"risk_class": 4, "input_schema": {"type": "object", "properties": {}}},
}


def body(
    text_: str = "Hello", *, source: str = "transcript", purpose: str = "voice_turn", **extra: Any
) -> dict[str, Any]:
    return {
        "purpose": purpose,
        "system": "You are the Acme receptionist. Be brief and polite.",
        "messages": [{"role": "user", "content": [{"text": text_, "source": source}]}],
        **extra,
    }

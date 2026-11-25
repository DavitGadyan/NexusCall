"""Code-defined adapters for the internal tools (BUILD_CONVENTIONS §5).

The downstream service, method and path live in code. The registry's `target` must match exactly, so a
changed catalogue row can never redirect a tool to another endpoint. Path parameters for the conversation's
contact and call come ONLY from the verified context; the request body never carries a tenant or actor
(those travel in the authenticated headers). Outputs are projected onto the contract fields before the
registered output schema is applied.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote


class ArgumentRejected(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Request:
    path: str
    params: dict[str, Any] | None = None
    body: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class InternalAdapter:
    tool_id: str
    service: str
    method: str
    path: str
    needs: frozenset[str]
    build: Callable[[dict[str, Any], dict[str, str], str], Request]
    project: Callable[[Any], Any]

    def matches(self, target: dict[str, Any]) -> bool:
        return (
            target.get("kind") == "internal"
            and target.get("service") == self.service
            and target.get("method") == self.method
            and target.get("path") == self.path
        )


def _p(value: str) -> str:
    return quote(str(value), safe="")


def _dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArgumentRejected("invalid timestamp") from exc


def _range(start: str, end: str, max_span: timedelta) -> None:
    a, b = _dt(start), _dt(end)
    if b <= a:
        raise ArgumentRejected("the end must be after the start")
    if b - a > max_span:
        raise ArgumentRejected("the time range is too long")


def _pick(obj: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(obj, dict):
        return {}
    return {k: obj.get(k) for k in keys if k in obj}


def _source(execution_id: str) -> dict[str, str]:
    return {"type": "ai_tool", "id": execution_id}


def _knowledge(args: dict[str, Any], ctx: dict[str, str], eid: str) -> Request:
    return Request("/internal/v1/knowledge/search", body={"query": args["query"], "top_k": 5})


def _knowledge_out(data: Any) -> Any:
    chunks = data.get("chunks", []) if isinstance(data, dict) else []
    keys = ("chunk_id", "source_id", "source_name", "text", "score")
    return {
        "chunks": [
            {k: (str(v) if k.endswith("_id") and v is not None else v) for k, v in _pick(c, keys).items()}
            for c in chunks[:8]
        ]
    }


def _slots(args: dict[str, Any], ctx: dict[str, str], eid: str) -> Request:
    _range(args["from"], args["to"], timedelta(days=31))
    return Request("/internal/v1/availability", params={"from": args["from"], "to": args["to"]})


def _slots_out(data: Any) -> Any:
    slots = data.get("slots", []) if isinstance(data, dict) else []
    return {"slots": [_pick(s, ("start", "end", "resource_id")) for s in slots[:50]]}


CONTACT_FIELDS = (
    "contact_id", "first_name", "last_name", "company", "timezone", "locale", "status", "email_masked", "phone_masked",
)  # fmt: skip


def _contact(args: dict[str, Any], ctx: dict[str, str], eid: str) -> Request:
    return Request(f"/internal/v1/contacts/{_p(ctx['contact_id'])}")


def _contact_out(data: Any) -> Any:
    # Masked identifiers only; custom fields and raw addresses never reach the model.
    out = _pick(data, CONTACT_FIELDS)
    out["contact_id"] = str(out.get("contact_id", ""))
    return out


def _note(args: dict[str, Any], ctx: dict[str, str], eid: str) -> Request:
    return Request(
        f"/internal/v1/contacts/{_p(ctx['contact_id'])}/notes", body={"text": args["text"], "source": _source(eid)}
    )


def _tentative(args: dict[str, Any], ctx: dict[str, str], eid: str) -> Request:
    _range(args["start"], args["end"], timedelta(hours=4))
    body = {"contact_id": ctx["contact_id"], "start": args["start"], "end": args["end"], "source": _source(eid)}
    return Request("/internal/v1/bookings/tentative", body=body)


def _transfer(args: dict[str, Any], ctx: dict[str, str], eid: str) -> Request:
    return Request(f"/internal/v1/calls/{_p(ctx['call_id'])}/transfer", body={"reason": args["reason"]})


def _confirm(args: dict[str, Any], ctx: dict[str, str], eid: str) -> Request:
    # The booking id is model-supplied: booking-service verifies it belongs to this tenant AND contact.
    body = {"contact_id": ctx["contact_id"], "source": _source(eid)}
    return Request(f"/internal/v1/bookings/{_p(args['booking_id'])}/confirm", body=body)


def _ids(keys: tuple[str, ...]) -> Callable[[Any], Any]:
    def project(data: Any) -> Any:
        return {k: (str(v) if v is not None else None) for k, v in _pick(data, keys).items()}

    return project


ADAPTERS: dict[str, InternalAdapter] = {
    a.tool_id: a
    for a in (
        InternalAdapter("knowledge.search", "ai-agent-service", "POST", "/internal/v1/knowledge/search",
                        frozenset(), _knowledge, _knowledge_out),
        InternalAdapter("booking.find_slots", "booking-service", "GET", "/internal/v1/availability",
                        frozenset(), _slots, _slots_out),
        InternalAdapter("crm.get_contact", "crm-service", "GET", "/internal/v1/contacts/{contact_id}",
                        frozenset({"contact_id"}), _contact, _contact_out),
        InternalAdapter("crm.add_note", "crm-service", "POST", "/internal/v1/contacts/{contact_id}/notes",
                        frozenset({"contact_id"}), _note, _ids(("note_id",))),
        InternalAdapter("booking.create_tentative", "booking-service", "POST", "/internal/v1/bookings/tentative",
                        frozenset({"contact_id"}), _tentative, _ids(("booking_id", "status", "start", "end"))),
        InternalAdapter("call.transfer_to_human", "call-service", "POST", "/internal/v1/calls/{call_id}/transfer",
                        frozenset({"call_id"}), _transfer, _ids(("status",))),
        InternalAdapter("booking.confirm", "booking-service", "POST", "/internal/v1/bookings/{booking_id}/confirm",
                        frozenset({"contact_id"}), _confirm, _ids(("booking_id", "status"))),
    )
}  # fmt: skip

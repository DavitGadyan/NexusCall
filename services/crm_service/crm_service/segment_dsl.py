"""Segment rule DSL → SQLAlchemy expression (bind parameters only; never string SQL).

    {"op": "and" | "or", "rules": [ {"field": ..., "operator": ..., "value": ...} | <nested group> ]}

Fields and operators come from allowlists. Custom fields (`custom.<key>`) are accepted only when the key
exists in the tenant's definitions, and the operators allowed depend on the definition's type.
Depth, rule count, list sizes and string lengths are bounded.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, case, exists, false, func, or_, select, true

from crm_service.tables import contact_tags, contacts, leads, tags

MAX_DEPTH = 4
MAX_RULES = 50
MAX_LIST = 100
MAX_STRING = 200
MAX_JSON_BYTES = 16_384

CONTACT_STATUSES = ("active", "inactive", "archived")
LEAD_STATUSES = ("new", "contacted", "qualified", "unqualified", "booked", "won", "lost")

STRING_OPS = frozenset({"eq", "neq", "in", "not_in", "contains", "starts_with", "is_set", "is_not_set"})
UUID_OPS = frozenset({"eq", "neq", "in", "not_in", "is_set", "is_not_set"})
ORDER_OPS = frozenset({"eq", "neq", "gt", "gte", "lt", "lte", "is_set", "is_not_set"})
ENUM_OPS = frozenset({"eq", "neq", "in", "not_in"})

FIELDS: dict[str, tuple[str, frozenset[str]]] = {
    "first_name": ("string", STRING_OPS),
    "last_name": ("string", STRING_OPS),
    "company": ("string", STRING_OPS),
    "email": ("string", STRING_OPS),
    "phone_e164": ("string", STRING_OPS),
    "timezone": ("string", STRING_OPS),
    "country": ("string", STRING_OPS),
    "locale": ("string", STRING_OPS),
    "source": ("string", STRING_OPS),
    "status": ("contact_status", ENUM_OPS),
    "owner_id": ("uuid", UUID_OPS),
    "created_at": ("datetime", frozenset({"gt", "gte", "lt", "lte"})),
    "updated_at": ("datetime", frozenset({"gt", "gte", "lt", "lte"})),
    "tag": ("tag", frozenset({"has", "has_any", "has_none"})),
    "lead_status": ("lead_status", frozenset({"eq", "in", "not_in"})),
}
CUSTOM_OPS: dict[str, frozenset[str]] = {
    "text": STRING_OPS,
    "number": ORDER_OPS,
    "boolean": frozenset({"eq", "is_set", "is_not_set"}),
    "date": ORDER_OPS,
    "select": frozenset({"eq", "neq", "in", "not_in", "is_set", "is_not_set"}),
}
_CUSTOM_KEY = re.compile(r"^custom\.([a-z][a-z0-9_]{0,63})$")
_NO_VALUE = frozenset({"is_set", "is_not_set"})
_LIST_OPS = frozenset({"in", "not_in", "has_any", "has_none"})


class SegmentError(ValueError):
    """Invalid rules. Messages name fields/operators only, never values."""


def _string(v: Any, where: str) -> str:
    if not isinstance(v, str) or not v or len(v) > MAX_STRING:
        raise SegmentError(f"{where}: value must be a string of 1..{MAX_STRING} characters")
    return v


def _list(v: Any, where: str) -> list[Any]:
    if not isinstance(v, list) or not v or len(v) > MAX_LIST:
        raise SegmentError(f"{where}: value must be a list of 1..{MAX_LIST} items")
    return v


def _datetime(v: Any, where: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_string(v, where))
    except ValueError as exc:
        raise SegmentError(f"{where}: value must be an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _date(v: Any, where: str) -> str:
    try:
        return date.fromisoformat(_string(v, where)).isoformat()
    except ValueError as exc:
        raise SegmentError(f"{where}: value must be an ISO date (YYYY-MM-DD)") from exc


def _number(v: Any, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise SegmentError(f"{where}: value must be a number")
    return float(v)


def _uuid(v: Any, where: str) -> uuid.UUID:
    try:
        return uuid.UUID(_string(v, where))
    except ValueError as exc:
        raise SegmentError(f"{where}: value must be a UUID") from exc


def _compare(col: Any, op: str, value: Any) -> ColumnElement[bool]:
    if op == "eq":
        return col == value  # type: ignore[no-any-return]
    if op == "neq":
        return col.is_distinct_from(value)  # type: ignore[no-any-return]
    if op == "gt":
        return col > value  # type: ignore[no-any-return]
    if op == "gte":
        return col >= value  # type: ignore[no-any-return]
    if op == "lt":
        return col < value  # type: ignore[no-any-return]
    if op == "lte":
        return col <= value  # type: ignore[no-any-return]
    raise SegmentError(f"unsupported operator {op}")


class Compiler:
    def __init__(self, custom_defs: dict[str, dict[str, Any]]) -> None:
        self.custom_defs = custom_defs  # key -> {"type": ..., "options": [...]}
        self.rule_count = 0

    def compile(self, rules: Any) -> ColumnElement[bool]:
        try:
            size = len(json.dumps(rules))
        except (TypeError, ValueError) as exc:
            raise SegmentError("rules must be JSON") from exc
        if size > MAX_JSON_BYTES:
            raise SegmentError("rules document is too large")
        return self._group(rules, depth=1)

    def _group(self, node: Any, depth: int) -> ColumnElement[bool]:
        if not isinstance(node, dict) or set(node) != {"op", "rules"}:
            raise SegmentError("a group must have exactly the keys 'op' and 'rules'")
        if depth > MAX_DEPTH:
            raise SegmentError(f"rules may be nested at most {MAX_DEPTH} levels deep")
        op, children = node["op"], node["rules"]
        if op not in ("and", "or"):
            raise SegmentError("group op must be 'and' or 'or'")
        if not isinstance(children, list):
            raise SegmentError("group rules must be a list")
        if not children:
            return true() if op == "and" else false()
        parts = [self._group(c, depth + 1) if "op" in c else self._rule(c) for c in self._dicts(children)]
        return and_(*parts) if op == "and" else or_(*parts)

    @staticmethod
    def _dicts(children: list[Any]) -> list[dict[str, Any]]:
        if not all(isinstance(c, dict) for c in children):
            raise SegmentError("every rule must be an object")
        return children

    def _rule(self, node: dict[str, Any]) -> ColumnElement[bool]:  # noqa: PLR0912 - one branch per field kind
        self.rule_count += 1
        if self.rule_count > MAX_RULES:
            raise SegmentError(f"at most {MAX_RULES} rules are allowed")
        allowed_keys = {"field", "operator", "value"}
        if not set(node) <= allowed_keys or not {"field", "operator"} <= set(node):
            raise SegmentError("a rule must have 'field', 'operator' and (usually) 'value'")
        field, op = node["field"], node["operator"]
        if not isinstance(field, str) or not isinstance(op, str):
            raise SegmentError("field and operator must be strings")
        where = f"rule {field}"
        if op in _NO_VALUE:
            if "value" in node and node["value"] is not None:
                raise SegmentError(f"{where}: operator {op} takes no value")
        elif "value" not in node:
            raise SegmentError(f"{where}: a value is required")
        value = node.get("value")
        if (m := _CUSTOM_KEY.match(field)) is not None:
            return self._custom(m.group(1), op, value, where)
        if field not in FIELDS:
            raise SegmentError(f"unknown field {field}")
        kind, ops = FIELDS[field]
        if op not in ops:
            raise SegmentError(f"{where}: operator {op} is not allowed")
        if kind == "tag":
            names = [_string(v, where) for v in _list(value, where)] if op in _LIST_OPS else [_string(value, where)]
            has = exists().where(
                contact_tags.c.tenant_id == contacts.c.tenant_id,
                contact_tags.c.contact_id == contacts.c.id,
                contact_tags.c.tag_id.in_(select(tags.c.id).where(tags.c.name.in_(names))),
            )
            return ~has if op == "has_none" else has
        if kind == "lead_status":
            statuses = self._enum_values(op, value, LEAD_STATUSES, where)
            has_lead = exists().where(
                leads.c.tenant_id == contacts.c.tenant_id,
                leads.c.contact_id == contacts.c.id,
                leads.c.status.in_(statuses),
            )
            return ~has_lead if op == "not_in" else has_lead
        col = contacts.c[field]
        if kind == "contact_status":
            values = self._enum_values(op, value, CONTACT_STATUSES, where)
            return col.in_(values) if op in ("eq", "in") else col.not_in(values)
        if kind == "datetime":
            return _compare(col, op, _datetime(value, where))
        if kind == "uuid":
            return self._scalar_ops(col, op, value, lambda v: _uuid(v, where), where)
        return self._string_ops(col, op, value, where, lower=field == "email")

    @staticmethod
    def _enum_values(op: str, value: Any, allowed: tuple[str, ...], where: str) -> list[str]:
        values = _list(value, where) if op in _LIST_OPS else [value]
        if any(v not in allowed for v in values):
            raise SegmentError(f"{where}: value must be one of {list(allowed)}")
        return [str(v) for v in values]

    @staticmethod
    def _scalar_ops(col: Any, op: str, value: Any, conv: Any, where: str) -> ColumnElement[bool]:
        if op == "is_set":
            return col.is_not(None)  # type: ignore[no-any-return]
        if op == "is_not_set":
            return col.is_(None)  # type: ignore[no-any-return]
        if op == "in":
            return col.in_([conv(v) for v in _list(value, where)])  # type: ignore[no-any-return]
        if op == "not_in":
            return or_(col.is_(None), col.not_in([conv(v) for v in _list(value, where)]))
        return _compare(col, op, conv(value))

    def _string_ops(self, col: Any, op: str, value: Any, where: str, *, lower: bool = False) -> ColumnElement[bool]:
        def conv(v: Any) -> str:
            s = _string(v, where)
            return s.lower() if lower else s

        if op == "contains":
            return col.icontains(conv(value), autoescape=True)  # type: ignore[no-any-return]
        if op == "starts_with":
            return col.istartswith(conv(value), autoescape=True)  # type: ignore[no-any-return]
        return self._scalar_ops(col, op, value, conv, where)

    def _custom(self, key: str, op: str, value: Any, where: str) -> ColumnElement[bool]:
        definition = self.custom_defs.get(key)
        if definition is None:
            raise SegmentError(f"unknown custom field {key}")
        ftype = definition["type"]
        if op not in CUSTOM_OPS[ftype]:
            raise SegmentError(f"{where}: operator {op} is not allowed for {ftype} fields")
        doc = contacts.c.custom_fields
        if op == "is_set":
            return doc.has_key(key)  # type: ignore[no-any-return]
        if op == "is_not_set":
            return ~doc.has_key(key)  # type: ignore[no-any-return]
        element = doc[key]
        typed = func.jsonb_typeof(element)
        if ftype == "number":
            num = case((typed == "number", element.as_float()), else_=None)
            return _compare(num, op, _number(value, where))
        if ftype == "boolean":
            if not isinstance(value, bool):
                raise SegmentError(f"{where}: value must be true or false")
            return and_(typed == "boolean", element.as_boolean() == value)
        text_value = case((typed == "string", element.astext), else_=None)
        if ftype == "date":
            return _compare(text_value, op, _date(value, where))
        if ftype == "select":
            options = set(definition.get("options") or [])
            values = _list(value, where) if op in _LIST_OPS else [value]
            if any(v not in options for v in values):
                raise SegmentError(f"{where}: value must be one of the field's options")
            return self._scalar_ops(text_value, op, value, lambda v: v, where)
        return self._string_ops(text_value, op, value, where)


def compile_rules(rules: Any, custom_defs: dict[str, dict[str, Any]]) -> ColumnElement[bool]:
    return Compiler(custom_defs).compile(rules)

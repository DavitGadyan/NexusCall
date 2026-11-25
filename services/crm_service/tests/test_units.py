"""Pure unit tests (no database): normalization, masking, segment DSL limits, custom fields, CSV safety."""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from crm_service.custom_fields import CustomFieldError, validate_custom_fields
from crm_service.jobs import RowError, parse_row, safe_cell
from crm_service.normalize import InvalidAddress, mask_email, mask_phone, normalize_email, normalize_phone, value_hash
from crm_service.segment_dsl import MAX_DEPTH, MAX_RULES, SegmentError, compile_rules

DEFS = {
    "plan": {"type": "select", "options": ["basic", "pro"], "required": False},
    "seats": {"type": "number", "options": [], "required": False},
    "vip": {"type": "boolean", "options": [], "required": False},
    "renewal": {"type": "date", "options": [], "required": False},
    "notes": {"type": "text", "options": [], "required": False},
}


def sql(expr) -> str:
    return str(expr.compile(dialect=postgresql.dialect()))


# ------------------------------------------------------------------------------------- normalization
def test_email_and_phone_normalization():
    assert normalize_email("  Ada@Example.TEST ") == "ada@example.test"
    assert normalize_phone("(212) 555-0123", "US") == "+12125550123"
    assert normalize_phone("+1 312 555 0140", "GB") == "+13125550140"
    for bad in ("not-an-email", "a@b", "a@@b.test", "a b@c.test", "x@y..test", "line\n@x.test"):
        with pytest.raises(InvalidAddress):
            normalize_email(bad)
    for bad in ("12", "+999123", "abc", "+1555012"):
        with pytest.raises(InvalidAddress):
            normalize_phone(bad, "US")


def test_hash_and_masks_hide_pii():
    assert value_hash("ada@example.test") == value_hash("ada@example.test")
    assert len(value_hash("x")) == 64
    assert mask_email("ada@example.test") == "a***@example.test"
    masked = mask_phone("+12125550123")
    assert masked.endswith("0123") and "555" not in masked


# ----------------------------------------------------------------------------------------- segments
def test_dsl_compiles_with_bind_parameters_only():
    rules = {
        "op": "and",
        "rules": [
            {"field": "last_name", "operator": "eq", "value": "x'; DROP TABLE contacts; --"},
            {"op": "or", "rules": [{"field": "tag", "operator": "has_any", "value": ["vip"]}]},
            {"field": "custom.seats", "operator": "gte", "value": 5},
        ],
    }
    text = sql(compile_rules(rules, DEFS))
    assert "DROP TABLE" not in text and "%(" in text


@pytest.mark.parametrize(
    ("rules", "fragment"),
    [
        ({"op": "xor", "rules": []}, "op must be"),
        ({"op": "and", "rules": [{"field": "password", "operator": "eq", "value": "x"}]}, "unknown field"),
        ({"op": "and", "rules": [{"field": "email", "operator": "gt", "value": "x"}]}, "not allowed"),
        ({"op": "and", "rules": [{"field": "custom.unknown", "operator": "eq", "value": "x"}]}, "unknown custom"),
        ({"op": "and", "rules": [{"field": "custom.plan", "operator": "eq", "value": "gold"}]}, "options"),
        ({"op": "and", "rules": [{"field": "custom.seats", "operator": "contains", "value": "1"}]}, "not allowed"),
        ({"op": "and", "rules": [{"field": "status", "operator": "eq", "value": "deleted"}]}, "one of"),
        ({"op": "and", "rules": [{"field": "created_at", "operator": "gt", "value": "yesterday"}]}, "ISO"),
        ({"op": "and", "rules": [{"field": "tag", "operator": "has_any", "value": []}]}, "list"),
        ({"op": "and", "rules": [{"field": "owner_id", "operator": "eq", "value": "nope"}]}, "UUID"),
        ({"op": "and", "rules": [{"field": "email", "operator": "eq", "value": "x", "sql": "1=1"}]}, "a rule"),
        ({"op": "and", "rules": [{"field": "email", "operator": "is_set", "value": "x"}]}, "takes no value"),
        ({"op": "and", "rules": ["raw"]}, "object"),
    ],
)
def test_dsl_rejects_invalid_rules(rules, fragment):
    with pytest.raises(SegmentError, match=fragment):
        compile_rules(rules, DEFS)


def test_dsl_depth_size_and_list_limits():
    deep = {"op": "and", "rules": []}
    for _ in range(MAX_DEPTH):
        deep = {"op": "and", "rules": [deep]}
    with pytest.raises(SegmentError, match="nested"):
        compile_rules(deep, DEFS)
    many = {"op": "or", "rules": [{"field": "source", "operator": "eq", "value": "x"}] * (MAX_RULES + 1)}
    with pytest.raises(SegmentError, match="at most"):
        compile_rules(many, DEFS)
    with pytest.raises(SegmentError, match="list"):
        compile_rules({"op": "and", "rules": [{"field": "country", "operator": "in", "value": ["US"] * 101}]}, DEFS)
    with pytest.raises(SegmentError, match="1..200"):
        compile_rules({"op": "and", "rules": [{"field": "company", "operator": "eq", "value": "x" * 201}]}, DEFS)


def test_dsl_contains_escapes_like_wildcards():
    text = str(
        compile_rules(
            {"op": "and", "rules": [{"field": "company", "operator": "contains", "value": "100%_"}]}, DEFS
        ).compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "100/%%/_" in text and "ESCAPE" in text  # % doubled by the pyformat paramstyle


# ------------------------------------------------------------------------------------- custom fields
def test_custom_field_validation():
    assert validate_custom_fields({"plan": "pro", "seats": 3}, DEFS) == {"plan": "pro", "seats": 3}
    assert validate_custom_fields({"seats": "12", "vip": "yes"}, DEFS, from_text=True) == {"seats": 12, "vip": True}
    assert validate_custom_fields({"seats": None}, DEFS, existing={"seats": 2}) == {}
    for bad in ({"plan": "gold"}, {"seats": True}, {"vip": "maybe"}, {"renewal": "13/2026"}, {"nope": 1}):
        with pytest.raises(CustomFieldError):
            validate_custom_fields(bad, DEFS)
    required = {**DEFS, "region": {"type": "text", "options": [], "required": True}}
    with pytest.raises(CustomFieldError, match="required"):
        validate_custom_fields({}, required)
    assert validate_custom_fields({}, required, existing={}) == {}  # not enforced on later edits


# ------------------------------------------------------------------------------------------- imports
def test_parse_row_validates_and_normalizes():
    row = parse_row(
        {"email": " Ada@Example.test", "phone": "212 555 0123", "first_name": "Ada", "custom.plan": "pro"}, "US"
    )
    assert row["email"] == "ada@example.test" and row["phone_e164"] == "+12125550123"
    assert row["custom"] == {"plan": "pro"}
    for raw, code in (
        ({"email": "bad"}, "invalid_email"),
        ({"phone": "123"}, "invalid_phone"),
        ({"first_name": "x"}, "missing_email_or_phone"),
        ({"email": "a@b.test", "timezone": "Mars/Base"}, "invalid_timezone"),
        ({"email": "a@b.test", "first_name": "a\rb"}, "invalid_first_name"),
    ):
        with pytest.raises(RowError, match=code):
            parse_row(raw, "US")


def test_export_cells_are_formula_safe():
    assert safe_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert safe_cell("@SUM(A1)") == "'@SUM(A1)"
    assert safe_cell("+12125550123") == "'+12125550123"
    assert safe_cell("Acme") == "Acme" and safe_cell(None) == ""

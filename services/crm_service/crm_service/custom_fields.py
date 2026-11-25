"""Custom field definitions and validation of contact `custom_fields` against them."""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from crm_service.tables import custom_field_defs

MAX_TEXT = 500
MAX_FIELDS = 50


class CustomFieldError(ValueError):
    """Messages name keys only, never values."""


async def load_defs(session: AsyncSession) -> dict[str, dict[str, Any]]:
    rows = (
        await session.execute(
            select(
                custom_field_defs.c.key,
                custom_field_defs.c.type,
                custom_field_defs.c.options,
                custom_field_defs.c.required,
            )
        )
    ).all()
    return {r.key: {"type": r.type, "options": list(r.options or []), "required": r.required} for r in rows}


def coerce_value(  # noqa: PLR0912 - one branch per field type
    definition: dict[str, Any], key: str, value: Any, *, from_text: bool = False
) -> Any:
    """Validate (and, for CSV input, convert from text) one value."""
    ftype = definition["type"]
    if ftype == "text":
        if not isinstance(value, str) or len(value) > MAX_TEXT:
            raise CustomFieldError(f"custom field {key}: expected text up to {MAX_TEXT} characters")
        return value
    if ftype == "number":
        if from_text and isinstance(value, str):
            try:
                value = float(value) if any(c in value for c in ".eE") else int(value)
            except ValueError as exc:
                raise CustomFieldError(f"custom field {key}: expected a number") from exc
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise CustomFieldError(f"custom field {key}: expected a number")
        return value
    if ftype == "boolean":
        if from_text and isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "yes", "1"):
                return True
            if lowered in ("false", "no", "0"):
                return False
        if not isinstance(value, bool):
            raise CustomFieldError(f"custom field {key}: expected true or false")
        return value
    if ftype == "date":
        try:
            return date.fromisoformat(str(value)).isoformat()
        except ValueError as exc:
            raise CustomFieldError(f"custom field {key}: expected an ISO date") from exc
    if ftype == "select":
        if value not in definition.get("options", []):
            raise CustomFieldError(f"custom field {key}: value is not one of the options")
        return value
    raise CustomFieldError(f"custom field {key}: unsupported type")


def validate_custom_fields(
    values: dict[str, Any],
    defs: dict[str, dict[str, Any]],
    *,
    existing: dict[str, Any] | None = None,
    from_text: bool = False,
) -> dict[str, Any]:
    """Return the merged, validated document. `None` removes a key. Unknown keys are rejected."""
    merged = dict(existing or {})
    if len(values) > MAX_FIELDS:
        raise CustomFieldError(f"at most {MAX_FIELDS} custom fields")
    for key, value in values.items():
        if key not in defs:
            raise CustomFieldError(f"unknown custom field {key}")
        if from_text and (value is None or value == ""):
            continue  # an empty CSV cell means "not provided", never "clear"
        if value is None:
            merged.pop(key, None)
            continue
        merged[key] = coerce_value(defs[key], key, value, from_text=from_text)
    # Required fields are enforced when a contact is created; definitions added later do not block edits.
    missing = [k for k, d in defs.items() if d.get("required") and k not in merged] if existing is None else []
    if missing:
        raise CustomFieldError(f"required custom fields missing: {sorted(missing)}")
    return merged

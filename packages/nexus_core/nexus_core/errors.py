"""RFC 7807 problem responses — one consistent error shape across services (build.md §8.1)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from nexus_core.context import request_id_var

log = logging.getLogger(__name__)


class ProblemError(Exception):
    status: int = 400
    code: str = "bad_request"
    title: str = "Bad request"

    def __init__(self, detail: str | None = None, *, extra: dict[str, Any] | None = None) -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.extra = extra or {}


class Unauthorized(ProblemError):
    status, code, title = 401, "unauthorized", "Authentication required"


class Forbidden(ProblemError):
    status, code, title = 403, "forbidden", "Permission denied"


class NotFound(ProblemError):
    status, code, title = 404, "not_found", "Resource not found"


class Conflict(ProblemError):
    status, code, title = 409, "conflict", "Conflict"


class Unprocessable(ProblemError):
    status, code, title = 422, "unprocessable", "Unprocessable request"


class TooManyRequests(ProblemError):
    status, code, title = 429, "rate_limited", "Too many requests"


class StepUpRequired(ProblemError):
    status, code, title = 401, "step_up_required", "Stronger authentication required"


class ServiceUnavailable(ProblemError):
    status, code, title = 503, "unavailable", "Dependency unavailable"


def problem(status: int, code: str, title: str, detail: str, **extra: Any) -> JSONResponse:
    body = {
        "type": f"https://errors.nexuscall.dev/{code}",
        "title": title,
        "status": status,
        "code": code,
        "detail": detail,
        "request_id": request_id_var.get(),
        **extra,
    }
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem(_: Request, exc: ProblemError) -> JSONResponse:
        return problem(exc.status, exc.code, exc.title, exc.detail, **exc.extra)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(exc.status_code, "http_error", "HTTP error", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo input values back (may contain PII); report locations and types only.
        errors = [{"loc": list(e.get("loc", ())), "type": e.get("type"), "msg": e.get("msg")} for e in exc.errors()]
        return problem(422, "validation_error", "Validation failed", "Request validation failed", errors=errors)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", extra={"error_type": type(exc).__name__})
        return problem(500, "internal_error", "Internal error", "An unexpected error occurred")

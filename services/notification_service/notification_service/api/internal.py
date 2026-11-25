"""Service-to-service API (BUILD_CONVENTIONS §5): allow-listed service identities only (`azp` check)."""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus_core.authz import InternalContext, require_internal
from nexus_core.errors import ServiceUnavailable
from notification_service.domain import KIND_RE, LINK_RE, PERMISSION_RE, create_notification, publish_hint
from notification_service.mailer import Mailer
from notification_service.settings import get_settings

router = APIRouter(prefix="/internal/v1/notifications", tags=["internal"], include_in_schema=False)
_guard = require_internal(*get_settings().internal_client_allowlist)


class EmailIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    to: str = Field(min_length=3, max_length=254)
    category: Literal["transactional"] = "transactional"
    variables: dict[str, str] = Field(default_factory=dict, max_length=20)


class InAppIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: uuid.UUID | None = None
    permission: str | None = Field(None, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=1000)
    link: str | None = Field(None, max_length=300)
    kind: str = Field("generic", max_length=32)

    @model_validator(mode="after")
    def _check(self) -> InAppIn:
        if (self.user_id is None) == (self.permission is None):
            raise ValueError("exactly one of user_id or permission is required")
        if self.permission is not None and not PERMISSION_RE.match(self.permission):
            raise ValueError("permission must be a catalogue permission key (no wildcards)")
        if not KIND_RE.match(self.kind):
            raise ValueError("invalid kind")
        if self.link is not None and not LINK_RE.match(self.link):
            raise ValueError("link must be an application-relative path")
        return self


def _dedupe_key(caller: str, idempotency_key: str | None) -> str | None:
    if not idempotency_key:
        return None
    return f"ik:{caller[:64]}:{hashlib.sha256(idempotency_key.encode()).hexdigest()[:40]}"


def _mailer(request: Request) -> Mailer:
    mailer: Mailer | None = getattr(request.app.state, "mailer", None)
    if mailer is None:
        raise ServiceUnavailable("Email delivery is not configured")
    return mailer


@router.post("/email", status_code=202)
async def send_email(body: EmailIn, request: Request, ctx: InternalContext = Depends(_guard)) -> JSONResponse:
    message_id = await _mailer(request).send(
        request.app.state.db,
        getattr(request.app.state, "redis", None),
        template=body.template,
        to=body.to,
        variables=body.variables,
        tenant_id=ctx.tenant_id,
        requested_by=ctx.principal.azp or "unknown",
    )
    return JSONResponse({"status": "sent", "provider_message_id": message_id}, status_code=202)


@router.post("/in-app", status_code=202)
async def create_in_app(
    body: InAppIn,
    request: Request,
    ctx: InternalContext = Depends(_guard),
    idempotency_key: Annotated[str | None, Header(max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")] = None,
) -> dict[str, Any]:
    tenant_id = ctx.require_tenant()
    caller = ctx.principal.azp or "unknown"
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as s:
        nid = await create_notification(
            s,
            tenant_id=tenant_id,
            user_id=body.user_id,
            permission=body.permission,
            kind=body.kind,
            title=body.title,
            body=body.body,
            link=body.link,
            source=caller.removeprefix("svc-"),
            dedupe_key=_dedupe_key(caller, idempotency_key),
        )
    if nid is not None:
        await publish_hint(
            getattr(request.app.state, "redis", None),
            tenant_id,
            nid,
            kind=body.kind,
            user_id=body.user_id,
            permission=body.permission,
        )
    return {"status": "accepted", "id": str(nid) if nid else None, "duplicate": nid is None}

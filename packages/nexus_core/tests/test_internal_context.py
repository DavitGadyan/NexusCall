import uuid
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from nexus_core.authz import InternalContext, require_internal
from nexus_core.errors import install_error_handlers
from nexus_core.testing import TokenFactory, auth


def _app(tokens: TokenFactory) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.state.token_verifier = tokens.verifier()

    @app.get("/internal/v1/thing")
    async def thing(ctx: Annotated[InternalContext, Depends(require_internal("svc-campaign-service"))]):
        return {"tenant": str(ctx.require_tenant()), "actor": str(ctx.actor), "caller": ctx.principal.azp}

    return app


def test_internal_context_parses_tenant_and_actor():
    tokens = TokenFactory()
    tenant = uuid.uuid4()
    with TestClient(_app(tokens)) as c:
        good = tokens.service("svc-campaign-service")
        r = c.get("/internal/v1/thing", headers=auth(good, tenant, **{"X-Actor": "user:abc"}))
        assert r.status_code == 200 and r.json() == {
            "tenant": str(tenant),
            "actor": "user:abc",
            "caller": "svc-campaign-service",
        }
        r = c.get("/internal/v1/thing", headers=auth(good, tenant))
        assert r.json()["actor"] == "service:svc-campaign-service"
        assert c.get("/internal/v1/thing", headers=auth(good)).status_code == 403  # tenant required
        assert c.get("/internal/v1/thing", headers=auth(good, "not-a-uuid")).status_code == 403
        assert c.get("/internal/v1/thing", headers=auth(good, tenant, **{"X-Actor": "root"})).status_code == 403
        # Other services and end users are rejected even with a valid token.
        assert c.get("/internal/v1/thing", headers=auth(tokens.service("svc-other"), tenant)).status_code == 403
        user = tokens.user(uuid.uuid4(), "u@example.test")
        assert c.get("/internal/v1/thing", headers=auth(user, tenant)).status_code == 403
        assert c.get("/internal/v1/thing").status_code == 401

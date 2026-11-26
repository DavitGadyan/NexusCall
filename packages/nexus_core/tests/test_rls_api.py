"""The internal RLS coverage route takes no query parameters; only the service guard protects it.

Regression: the guard sat inside `Annotated[...]` while referring to a local variable, so FastAPI could
not resolve it and required a query parameter named `_` instead. Every platform RLS scan got HTTP 422.
"""

from __future__ import annotations

from nexus_core.config import ServiceSettings
from nexus_core.rls_api import rls_router


def _settings() -> ServiceSettings:
    return ServiceSettings(service_name="crm-service", env="test", database_url="postgresql+asyncpg://u:p@db/crm")


def test_coverage_route_has_no_query_parameters() -> None:
    router = rls_router(_settings())
    route = next(r for r in router.routes if getattr(r, "path", "") == "/internal/v1/rls/coverage")
    assert [p.name for p in route.dependant.query_params] == []  # type: ignore[attr-defined]


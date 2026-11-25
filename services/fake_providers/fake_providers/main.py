from __future__ import annotations

import os

from fastapi import FastAPI

from fake_providers.routers import routers
from nexus_core.config import Environment, ServiceSettings
from nexus_core.errors import install_error_handlers
from nexus_core.health import health_router
from nexus_core.logging import configure_logging


class FakeProvidersInProduction(RuntimeError):
    pass


def create_app() -> FastAPI:
    settings = ServiceSettings(service_name="fake-providers")
    if settings.env not in (Environment.DEVELOPMENT, Environment.TEST) or os.environ.get("KUBERNETES_SERVICE_HOST"):
        raise FakeProvidersInProduction("fake-providers may only run in development/test (never in a cluster)")
    configure_logging("fake-providers", settings.env.value, settings.log_level)
    app = FastAPI(title="NexusCall FAKE providers (dev only)", version="0.1.0")
    install_error_handlers(app)
    app.include_router(health_router("fake-providers", "0.1.0", {}))
    for r in routers:
        app.include_router(r)
    return app

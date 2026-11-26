from fastapi import APIRouter

from integration_service.api import connections, inbound, internal, platform, webhooks

routers: list[APIRouter] = [
    connections.router,
    connections.callback_router,
    webhooks.router,
    platform.router,
    inbound.router,
    internal.router,
]

from fastapi import APIRouter

from tenant_service.api import internal, invitations, me, platform, team, tenant_settings

routers: list[APIRouter] = [
    me.router,
    team.router,
    invitations.router,
    tenant_settings.router,
    platform.router,
    internal.router,
]

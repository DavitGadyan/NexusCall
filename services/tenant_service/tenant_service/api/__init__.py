from fastapi import APIRouter

from tenant_service.api import internal, invitations, me, platform, platform_infra, team, tenant_settings

routers: list[APIRouter] = [
    me.router,
    team.router,
    invitations.router,
    tenant_settings.router,
    platform.router,
    platform_infra.router,
    internal.router,
]

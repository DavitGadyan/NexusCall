from fastapi import APIRouter

from ai_gateway.api import admin, internal

routers: list[APIRouter] = [internal.router, admin.tenant_router, admin.platform_router]

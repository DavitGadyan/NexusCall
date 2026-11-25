from fastapi import APIRouter

from mcp_registry.api import internal, platform, tenant

routers: list[APIRouter] = [tenant.router, platform.router, internal.router]

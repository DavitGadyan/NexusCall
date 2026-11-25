from fastapi import APIRouter

from audit_service.api import events, platform

routers: list[APIRouter] = [events.router, platform.router]

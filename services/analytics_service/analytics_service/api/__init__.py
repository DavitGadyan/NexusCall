from fastapi import APIRouter

from analytics_service.api import analytics, exports, platform

routers: list[APIRouter] = [analytics.router, exports.router, platform.router]

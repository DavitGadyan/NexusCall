from fastapi import APIRouter

from booking_service.api import internal, public

routers: list[APIRouter] = [public.resources_router, public.router, internal.router]

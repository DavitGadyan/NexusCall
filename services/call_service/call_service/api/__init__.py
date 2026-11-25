from fastapi import APIRouter

from call_service.api import calls, internal, numbers, webhooks

routers: list[APIRouter] = [calls.router, numbers.router, internal.router, webhooks.router]

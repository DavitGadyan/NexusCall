from fastapi import APIRouter

from campaign_service.api import campaigns, internal

routers: list[APIRouter] = [campaigns.router, internal.router]

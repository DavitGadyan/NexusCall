from fastapi import APIRouter

from voice_service.api import media, public

routers: list[APIRouter] = [media.router, public.router]

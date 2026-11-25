from fastapi import APIRouter

from notification_service.api import internal, notifications, stream

# `stream` before `notifications` so `/stream` is matched before any parameterised path.
routers: list[APIRouter] = [stream.router, notifications.router, internal.router]

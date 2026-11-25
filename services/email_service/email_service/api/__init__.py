from fastapi import APIRouter

from email_service.api import campaigns, domains, internal, messages, templates, unsubscribe, webhooks

routers: list[APIRouter] = [
    domains.router,
    templates.router,
    campaigns.router,
    messages.router,
    internal.router,
    webhooks.router,
    unsubscribe.router,
]

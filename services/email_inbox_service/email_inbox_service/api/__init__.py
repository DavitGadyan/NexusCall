from fastapi import APIRouter

from email_inbox_service.api import drafts, inbox, mailboxes, webhooks

routers: list[APIRouter] = [inbox.router, drafts.router, mailboxes.router, webhooks.router]

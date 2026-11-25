from fastapi import APIRouter

from tool_execution.api import approvals, internal

routers: list[APIRouter] = [approvals.router, internal.router]

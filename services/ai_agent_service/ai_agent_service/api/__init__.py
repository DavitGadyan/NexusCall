from fastapi import APIRouter

from ai_agent_service.api import agents, evals, internal, knowledge

routers: list[APIRouter] = [agents.router, knowledge.router, evals.router, internal.router]

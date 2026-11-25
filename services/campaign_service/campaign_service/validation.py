"""Pre-launch validation against other contexts: ai-agent (agent), crm (segment), tenant (limits)."""

from __future__ import annotations

import uuid
from typing import Any

from campaign_service.domain import Deps
from nexus_core.clients import UpstreamError, tenant_settings_section
from nexus_core.context import Actor

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def schedule_problems(campaign: dict[str, Any]) -> list[str]:
    """Local checks before a campaign can leave draft."""
    problems = []
    if campaign["agent_id"] is None:
        problems.append("agent_required")
    if campaign["segment_id"] is None:
        problems.append("segment_required")
    if campaign["window_start"] >= campaign["window_end"]:
        problems.append("invalid_window")
    return problems


def limit_problems(campaign: dict[str, Any], comm: dict[str, Any]) -> list[str]:
    problems = []
    if not comm.get("effective_calls_enabled", False):
        problems.append("calls_disabled_for_tenant")
    if campaign["max_concurrency"] > int(comm.get("max_concurrent_calls", 0)):
        problems.append("max_concurrency_exceeds_tenant_limit")
    if campaign["calls_per_minute"] > int(comm.get("max_daily_calls", 0)):
        problems.append("calls_per_minute_exceeds_tenant_daily_limit")
    window = comm.get("calling_window") or {}
    if window:
        if campaign["window_start"] < window.get("start", "00:00") or campaign["window_end"] > window.get(
            "end", "23:59"
        ):
            problems.append("window_outside_tenant_calling_window")
        if not set(campaign["window_days"]) <= set(window.get("days", DAYS)):
            problems.append("days_outside_tenant_calling_window")
    return problems


async def remote_problems(deps: Deps, tenant_id: uuid.UUID, actor: Actor, campaign: dict[str, Any]) -> list[str]:
    """Raises UpstreamUnavailable when a dependency is down (the launch is then retryable)."""
    problems: list[str] = []
    try:
        agent = await deps.client("ai-agent-service").get_json(
            f"/internal/v1/agents/{campaign['agent_id']}", tenant_id=tenant_id, actor=actor
        )
        if agent.get("kind") != "voice":
            problems.append("agent_not_voice")
        if agent.get("status") != "active":
            problems.append("agent_not_active")
    except UpstreamError as exc:
        if not exc.is_not_found:
            raise
        problems.append("agent_not_found")
    try:
        await deps.client("crm-service").post_json(
            f"/internal/v1/segments/{campaign['segment_id']}/resolve",
            {"limit": 1, "channel": "phone"},
            tenant_id=tenant_id,
            actor=actor,
        )
    except UpstreamError as exc:
        if not exc.is_not_found:
            raise
        problems.append("segment_not_found")
    comm = await tenant_settings_section(deps.client("tenant-service"), deps.redis, tenant_id, "communication")
    return problems + limit_problems(campaign, comm)

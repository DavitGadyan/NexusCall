"""Deterministic AI policy decisions (OWASP LLM02/LLM03/LLM04/LLM06). Nothing here asks a model anything."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_gateway.catalog import CLASS_RANK, cost_micros
from ai_gateway.tables import (
    ai_budget_policies,
    ai_kill_switches,
    ai_models,
    ai_usage_records,
    routing_policies,
    tenant_model_allowlists,
)
from nexus_core.errors import ProblemError


class PolicyDenied(ProblemError):
    status, code, title = 403, "policy_denied", "Denied by AI policy"

    def __init__(self, reason: str, detail: str | None = None, *, status: int = 403) -> None:
        super().__init__(detail or reason, extra={"reason": reason})
        self.reason = reason
        self.status = status


@dataclass(frozen=True, slots=True)
class ModelInfo:
    model_id: str
    provider: str
    in_price: int
    out_price: int
    max_output_tokens: int


def period_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


async def kill_switch_reason(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    provider: str | None = None,
    model_id: str | None = None,
) -> str | None:
    targets = [("platform", "*"), ("tenant", str(tenant_id))]
    if agent_id:
        targets.append(("agent", str(agent_id)))
    if provider:
        targets.append(("provider", provider))
    if model_id:
        targets.append(("model", model_id))
    rows = (
        (
            await session.execute(
                select(ai_kill_switches.c.scope).where(
                    ai_kill_switches.c.engaged.is_(True),
                    func.row(ai_kill_switches.c.scope, ai_kill_switches.c.target).in_(targets),
                )
            )
        )
        .scalars()
        .all()
    )
    return f"kill_switch_{sorted(rows)[0]}" if rows else None


async def resolve_models(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    purpose: str,
    requested: str | None,
    classification: str,
    tenant_allowed: list[str],
    agent_id: uuid.UUID | None,
) -> tuple[list[ModelInfo], bool]:
    """Ordered candidate models that pass every policy, plus whether failover between them is allowed."""
    route = (
        (await session.execute(select(routing_policies).where(routing_policies.c.purpose == purpose)))
        .mappings()
        .first()
    )
    if route is None:
        raise PolicyDenied("no_route", "No routing policy for this purpose")
    order: list[str] = [requested] if requested else list(route["models"])
    failover = bool(route["failover_allowed"]) and requested is None
    rows = {
        r["model_id"]: r
        for r in (await session.execute(select(ai_models).where(ai_models.c.model_id.in_(order)))).mappings().all()
    }
    gateway_allow = set((await session.execute(select(tenant_model_allowlists.c.model_id))).scalars())
    out: list[ModelInfo] = []
    for model_id in order:
        row = rows.get(model_id)
        if row is None or not row["enabled"] or purpose not in row["purposes"]:
            continue
        if classification not in row["allowed_classifications"]:
            continue
        if model_id not in tenant_allowed:  # tenant settings allowlist (tenant-service)
            continue
        if gateway_allow and model_id not in gateway_allow:  # platform-managed per-tenant allowlist
            continue
        if await kill_switch_reason(
            session, tenant_id=tenant_id, agent_id=agent_id, provider=row["provider"], model_id=model_id
        ):
            continue
        out.append(
            ModelInfo(
                model_id,
                row["provider"],
                int(row["input_price_micros_per_mtok"]),
                int(row["output_price_micros_per_mtok"]),
                int(row["max_output_tokens"]),
            )
        )
    if not out:
        raise PolicyDenied("model_not_allowed", "No approved model is available for this request")
    return out, failover


def check_classification(requested: str, tenant_max: str) -> None:
    if CLASS_RANK.get(requested, 99) > CLASS_RANK.get(tenant_max, -1):
        raise PolicyDenied(
            "classification_not_allowed",
            "This tenant does not allow sending data of this classification to a model provider",
        )


async def budget_state(
    session: AsyncSession, *, agent_id: uuid.UUID | None, tenant_budget_usd: int, default_request_cap: int
) -> dict[str, Any]:
    """Monthly spend so far and the effective limits (minimum of tenant settings and gateway policies)."""
    period = period_now()
    used_tenant = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(ai_usage_records.c.cost_micros), 0)).where(
                    ai_usage_records.c.period == period
                )
            )
        ).scalar_one()
    )
    used_agent = 0
    if agent_id:
        used_agent = int(
            (
                await session.execute(
                    select(func.coalesce(func.sum(ai_usage_records.c.cost_micros), 0)).where(
                        ai_usage_records.c.period == period, ai_usage_records.c.agent_key == str(agent_id)
                    )
                )
            ).scalar_one()
        )
    policies = (await session.execute(select(ai_budget_policies))).mappings().all()
    tenant_limit = tenant_budget_usd * 1_000_000
    agent_limit: int | None = None
    request_cap = default_request_cap
    for p in policies:
        if p["scope"] == "tenant":
            tenant_limit = min(tenant_limit, int(p["monthly_limit_micros"]))
            if p["per_request_limit_micros"] is not None:
                request_cap = min(request_cap, int(p["per_request_limit_micros"]))
        elif agent_id and p["agent_id"] == agent_id:
            agent_limit = int(p["monthly_limit_micros"])
            if p["per_request_limit_micros"] is not None:
                request_cap = min(request_cap, int(p["per_request_limit_micros"]))
    return {
        "period": period,
        "used_tenant": used_tenant,
        "used_agent": used_agent,
        "tenant_limit": tenant_limit,
        "agent_limit": agent_limit,
        "request_cap": request_cap,
    }


def worst_case_cost(model: ModelInfo, input_chars: int, max_tokens: int, rounds: int) -> int:
    """Upper-bound estimate before any provider call (4 chars per token is conservative for English)."""
    est_input = input_chars // 3 + 200
    return cost_micros(est_input * rounds, max_tokens * rounds, model.in_price, model.out_price)


def check_budget(state: dict[str, Any], estimate: int) -> None:
    if state["used_tenant"] + estimate > state["tenant_limit"]:
        raise PolicyDenied("budget_exceeded", "The tenant's monthly AI budget would be exceeded")
    if state["agent_limit"] is not None and state["used_agent"] + estimate > state["agent_limit"]:
        raise PolicyDenied("budget_exceeded", "The agent's monthly AI budget would be exceeded")


async def record_usage(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    input_tokens: int,
    output_tokens: int,
    cost: int,
    denied: bool,
) -> None:
    values = {
        "tenant_id": tenant_id,
        "period": period_now(),
        "agent_key": str(agent_id) if agent_id else "-",
        "requests": 0 if denied else 1,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_micros": cost,
        "denied": 1 if denied else 0,
    }
    t = ai_usage_records
    await session.execute(
        insert(t)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[t.c.tenant_id, t.c.period, t.c.agent_key],
            set_={
                "requests": t.c.requests + values["requests"],
                "input_tokens": t.c.input_tokens + input_tokens,
                "output_tokens": t.c.output_tokens + output_tokens,
                "cost_micros": t.c.cost_micros + cost,
                "denied": t.c.denied + values["denied"],
                "updated_at": func.now(),
            },
        )
    )

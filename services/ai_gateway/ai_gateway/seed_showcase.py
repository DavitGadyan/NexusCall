"""LOCAL DEVELOPMENT showcase data (see `nexus_core.devshowcase`): AI Gateway usage for the last 45 days.

* Acme: one `ai_requests` row per AI call behind the showcase (voice turns, post-call analysis, email triage and
  drafting), and monthly `ai_usage_records` per agent built from exactly those rows.
* Globex and the platform tenants: monthly usage per agent from the same synthetic history the analytics read
  model uses; Harbor & Pine runs close to a tight budget, so a few of its requests were denied.
* Monthly budget policies, and a modest number of older security events of the kinds the gateway raises.

Existing rows written by live traffic are never touched: usage rows are keyed by agent (live rows without an
agent use '-'), and inserts are ON CONFLICT DO NOTHING with fixed ids, so a second run changes nothing. Nothing
here stores prompt or completion text. Runs as `aigw_app`, so RLS applies.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_gateway.tables import ai_budget_policies, ai_requests, ai_security_events, ai_usage_records
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devseed import GLOBEX
from nexus_core.devshowcase import (
    EMAIL_AGENT_ID,
    GLOBEX_PROFILE,
    PLATFORM_TENANTS,
    TENANT,
    VOICE_AGENT_ID,
    PlatformTenant,
    Scenario,
    acme_ai_calls,
    sid,
    tenant_ai_usage,
)

SEED_ACTOR = Actor("system", "dev-showcase")
TIGHT_BUDGET = "harbor-pine-realty"
# tenant slug -> monthly budget (micros)
BUDGETS = {"acme": 25_000_000, "globex": 10_000_000, "brightside-dental": 10_000_000, "summit-solar": 15_000_000,
           TIGHT_BUDGET: 1_100_000, "northgate-legal": 5_000_000, "evergreen-plumbing": 10_000_000,
           "lakeview-dentistry": 5_000_000, "redwood-roofing": 5_000_000}  # fmt: skip
# (days ago, hour UTC, kind, severity, owasp, detail, tool id)
SECURITY_EVENTS = (
    (41, 15, "prompt_injection_suspected", "medium", "LLM01", "source=email signals=override_instructions", None),
    (36, 18, "tool_not_granted", "low", "LLM03", "requested tool crm.add_note is not granted", "crm.add_note"),
    (31, 14, "prompt_injection_suspected", "medium", "LLM01", "source=transcript signals=role_takeover", None),
    (27, 16, "tool_loop_detected", "medium", "LLM06", "booking.find_slots", "booking.find_slots"),
    (22, 19, "prompt_injection_suspected", "medium", "LLM01", "source=email signals=delimiter_spoof", None),
    (17, 13, "tool_denied_by_policy", "medium", "LLM03", "outside_business_hours", "booking.confirm"),
    (12, 20, "provider_circuit_open", "info", "LLM06", "anthropic", None),
    (8, 15, "prompt_injection_suspected", "medium", "LLM01", "source=email signals=exfiltration", None),
    (4, 17, "tool_not_granted", "low", "LLM03", "requested tool crm.add_note is not granted", "crm.add_note"),
)


def _period(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m")


async def seed(db: Database, sc: Scenario) -> None:
    calls = acme_ai_calls(sc)
    async with db.tenant_session(TENANT.id, SEED_ACTOR) as s:
        for start in range(0, len(calls), 200):
            await s.execute(
                insert(ai_requests)
                .values([
                    {"id": a.id, "tenant_id": TENANT.id, "caller": a.caller, "actor": "service:" + a.caller[4:],
                     "agent_id": a.agent_id, "purpose": a.purpose,
                     "classification": "confidential" if a.agent_id == EMAIL_AGENT_ID else "internal",
                     "model": a.model, "provider": a.provider, "status": a.status, "deny_reason": a.deny_reason,
                     "stop_reason": a.stop_reason, "rounds": a.rounds, "tool_calls": a.tool_calls,
                     "input_chars": a.input_chars, "untrusted_blocks": a.untrusted_blocks,
                     "input_tokens": a.input_tokens, "output_tokens": a.output_tokens, "cost_micros": a.cost_micros,
                     "latency_ms": a.latency_ms, "flags": a.flags, "input_fingerprint": a.id.hex[:32],
                     "created_at": a.at}
                    for a in calls[start : start + 200]
                ])  # fmt: skip
                .on_conflict_do_nothing()
            )
        usage: dict[tuple[str, str], list[int]] = {}
        for a in calls:
            row = usage.setdefault((_period(a.at), str(a.agent_id)), [0, 0, 0, 0, 0])
            row[0] += 1
            row[1] += a.input_tokens
            row[2] += a.output_tokens
            row[3] += a.cost_micros
        await _usage(s, TENANT.id, usage)
        await _budget(s, TENANT.id, "acme", sc)
        injected = [a for a in calls if "prompt_injection_suspected" in a.flags]
        for i, (ago, hour, kind, severity, owasp, detail, tool) in enumerate(SECURITY_EVENTS):
            at = datetime.combine(sc.anchor - timedelta(days=ago), time(hour, 7 * i % 60), UTC)
            request = injected.pop(0) if kind == "prompt_injection_suspected" and injected else None
            if request is not None:
                at = request.at + timedelta(milliseconds=300)
            agent = EMAIL_AGENT_ID if "email" in detail else VOICE_AGENT_ID
            await s.execute(
                insert(ai_security_events)
                .values(id=sid("ai_security_event", i + 1), tenant_id=TENANT.id,
                        request_id=request.id if request else None, kind=kind, severity=severity, owasp=owasp,
                        agent_id=agent, tool_id=tool, detail=detail, created_at=at)  # fmt: skip
                .on_conflict_do_nothing()
            )
    profiles: list[tuple[PlatformTenant, uuid.UUID]] = [(GLOBEX_PROFILE, GLOBEX.id)]
    profiles += [(t, t.id) for t in PLATFORM_TENANTS if t.status == "active"]
    for profile, tenant_id in profiles:
        async with db.tenant_session(tenant_id, SEED_ACTOR) as s:
            usage = {k: [*v, 0] for k, v in tenant_ai_usage(profile, sc.anchor).items()}
            if profile.slug == TIGHT_BUDGET:
                await _denials(s, profile, tenant_id, sc, usage)
            await _usage(s, tenant_id, usage)
            await _budget(s, tenant_id, profile.slug, sc)
            if profile.n in (0, 2, 5):  # a couple of signals on other tenants too
                at = datetime.combine(sc.anchor - timedelta(days=6 + profile.n), time(16, 40), UTC)
                await s.execute(
                    insert(ai_security_events)
                    .values(id=sid("ai_security_event", 100 + profile.n), tenant_id=tenant_id, kind=(
                        "prompt_injection_suspected"), severity="medium", owasp="LLM01",
                        agent_id=profile.email_agent_id, detail="source=email signals=override_instructions",
                        created_at=at)  # fmt: skip
                    .on_conflict_do_nothing()
                )


async def _usage(s: AsyncSession, tenant_id: uuid.UUID, usage: dict[tuple[str, str], list[int]]) -> None:
    for (period, agent_key), (req, tin, tout, cost, denied) in sorted(usage.items()):
        await s.execute(
            insert(ai_usage_records)
            .values(tenant_id=tenant_id, period=period, agent_key=agent_key, requests=req, input_tokens=tin,
                    output_tokens=tout, cost_micros=cost, denied=denied)  # fmt: skip
            .on_conflict_do_nothing()
        )


async def _budget(s: AsyncSession, tenant_id: uuid.UUID, slug: str, sc: Scenario) -> None:
    at = datetime.combine(sc.anchor - timedelta(days=44), time(15, 0), UTC)
    await s.execute(
        insert(ai_budget_policies)
        .values(id=sid("ai_budget", int(tenant_id.int % 10_000)), tenant_id=tenant_id, scope="tenant",
                monthly_limit_micros=BUDGETS[slug], per_request_limit_micros=50_000, updated_at=at)  # fmt: skip
        .on_conflict_do_nothing()
    )


async def _denials(s: AsyncSession, t: PlatformTenant, tenant_id: uuid.UUID, sc: Scenario,
                   usage: dict[tuple[str, str], list[int]]) -> None:  # fmt: skip
    """The last few days were over budget: those requests were denied (no cost, no tokens)."""
    period = sc.anchor.strftime("%Y-%m")
    for i in range(14):
        at = datetime.combine(sc.anchor - timedelta(days=1 + i % 3), time(15 + i % 5, 3 * i), UTC)
        agent = t.email_agent_id if i % 3 else t.voice_agent_id
        await s.execute(
            insert(ai_requests)
            .values(id=sid("ai_request_denied", t.n * 100 + i), tenant_id=tenant_id, caller=(
                "svc-ai-agent-service" if i % 3 else "svc-voice-service"), actor="service:ai-agent-service",
                agent_id=agent, purpose="email_draft" if i % 3 else "voice_turn", classification="internal",
                status="denied", deny_reason="budget_exceeded", created_at=at)  # fmt: skip
            .on_conflict_do_nothing()
        )
        row = usage.setdefault((period, str(agent)), [0, 0, 0, 0, 0])
        row[4] += 1

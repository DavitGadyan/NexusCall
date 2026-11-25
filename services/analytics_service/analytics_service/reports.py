"""Report assembly from `daily_metrics` and facts. All queries run inside the caller's tenant session."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Integer, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from analytics_service.tables import daily_metrics, fact_calls, fact_campaigns, fact_emails
from nexus_core.errors import Unprocessable


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date

    @property
    def days(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]


def date_range(start: date | None, end: date | None, *, max_days: int, default_days: int = 30) -> DateRange:
    end = end or datetime.now(UTC).date()
    start = start or end - timedelta(days=default_days - 1)
    if start > end:
        raise Unprocessable("'from' must not be after 'to'")
    if (end - start).days + 1 > max_days:
        raise Unprocessable(f"The date range may span at most {max_days} days")
    return DateRange(start, end)


def ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def per(total: int, count: int) -> int | None:
    return round(total / count) if count else None


async def metric_totals(session: AsyncSession, rng: DateRange, dimension: str = "") -> dict[str, int]:
    rows = (
        await session.execute(
            select(daily_metrics.c.metric, func.sum(daily_metrics.c.value))
            .where(
                daily_metrics.c.day >= rng.start,
                daily_metrics.c.day <= rng.end,
                daily_metrics.c.dimension == dimension,
            )
            .group_by(daily_metrics.c.metric)
        )
    ).all()
    return {metric: int(total or 0) for metric, total in rows}


def overview(totals: dict[str, int], rng: DateRange, *, email_unit_cost_micros: int) -> dict[str, Any]:
    t = totals.get
    attempted, answered = t("calls.attempted", 0), t("calls.answered", 0)
    ended, duration = t("calls.ended", 0), t("calls.duration_seconds", 0)
    accepted = t("emails.accepted", 0)
    approved, rejected, edited = t("drafts.approved", 0), t("drafts.rejected", 0), t("drafts.edited", 0)
    decided = approved + rejected
    call_cost, ai_cost, ai_requests = t("calls.cost_micros", 0), t("ai.cost_micros", 0), t("ai.requests", 0)
    email_cost = accepted * email_unit_cost_micros
    return {
        "from": rng.start,
        "to": rng.end,
        "calls": {
            "attempted": attempted,
            "answered": answered,
            "ended": ended,
            "completed": t("calls.completed", 0),
            "failed": t("calls.failed", 0),
            "canceled": t("calls.canceled", 0),
            "opted_out": t("calls.opted_out", 0),
            "qualified": t("calls.qualified", 0),
            "answer_rate": ratio(answered, attempted),
            "total_duration_seconds": duration,
            "average_duration_seconds": per(duration, ended),
        },
        "bookings": {
            "created": t("bookings.created", 0),
            "confirmed": t("bookings.confirmed", 0),
            "canceled": t("bookings.canceled", 0),
        },
        "emails": {
            "accepted": accepted,
            "delivered": t("emails.delivered", 0),
            "bounced": t("emails.bounced", 0),
            "complained": t("emails.complained", 0),
            "rejected": t("emails.rejected", 0),
            "failed": t("emails.failed", 0),
            "unsubscribed": t("emails.unsubscribed", 0),
            "delivery_rate": ratio(t("emails.delivered", 0), accepted),
            "bounce_rate": ratio(t("emails.bounced", 0), accepted),
            "complaint_rate": ratio(t("emails.complained", 0), accepted),
        },
        "drafts": {
            "requested": t("drafts.requested", 0),
            "generated": t("drafts.generated", 0),
            "approved": approved,
            "edited": edited,
            "rejected": rejected,
            "sent": t("drafts.sent", 0),
            "approved_rate": ratio(approved, decided),
            "rejected_rate": ratio(rejected, decided),
            "edited_rate": ratio(edited, approved),
        },
        "leads": {"created": t("leads.created", 0)},
        "campaigns": {"launched": t("campaigns.launched", 0)},
        "costs": {
            "call_cost_micros": call_cost,
            "cost_per_call_micros": per(call_cost, ended),
            "email_cost_micros": email_cost,
            "cost_per_email_micros": email_unit_cost_micros if accepted else None,
            "email_cost_estimated": True,
            "ai_cost_micros": ai_cost,
            "cost_per_ai_interaction_micros": per(ai_cost, ai_requests),
            "ai_input_tokens": t("ai.input_tokens", 0),
            "ai_output_tokens": t("ai.output_tokens", 0),
            "total_cost_micros": call_cost + email_cost + ai_cost,
        },
    }


async def timeseries(session: AsyncSession, metric: str, rng: DateRange, dimension: str) -> list[dict[str, Any]]:
    result = await session.execute(
        select(daily_metrics.c.day, daily_metrics.c.value).where(
            daily_metrics.c.metric == metric,
            daily_metrics.c.dimension == dimension,
            daily_metrics.c.day >= rng.start,
            daily_metrics.c.day <= rng.end,
        )
    )
    rows: dict[date, int] = {day: int(value) for day, value in result.all()}
    return [{"day": d, "value": int(rows.get(d, 0))} for d in rng.days]


def _count(flag: Any) -> Any:
    return func.coalesce(func.sum(case((flag, 1), else_=0).cast(Integer)), 0)


async def campaign_report(session: AsyncSession, campaign_id: uuid.UUID) -> dict[str, Any] | None:
    meta = (
        (
            await session.execute(
                select(fact_campaigns.c.channel, fact_campaigns.c.launched_at).where(fact_campaigns.c.id == campaign_id)
            )
        )
        .mappings()
        .first()
    )
    c = fact_calls.c
    calls = (
        (
            await session.execute(
                select(
                    func.count().label("attempted"),
                    _count(c.answered).label("answered"),
                    _count(c.completed).label("ended"),
                    _count(c.final_state == "completed").label("completed"),
                    _count(c.final_state == "failed").label("failed"),
                    _count(c.opted_out).label("opted_out"),
                    _count(c.qualified.is_(True)).label("qualified"),
                    func.coalesce(func.sum(c.duration_seconds), 0).label("duration"),
                    func.coalesce(func.sum(c.cost_micros), 0).label("cost"),
                ).where(c.campaign_id == campaign_id)
            )
        )
        .mappings()
        .one()
    )
    e = fact_emails.c
    emails = (
        (
            await session.execute(
                select(
                    func.count().label("messages"),
                    _count(e.accepted).label("accepted"),
                    _count(e.delivered).label("delivered"),
                    _count(e.bounced).label("bounced"),
                    _count(e.complained).label("complained"),
                    _count(e.unsubscribed).label("unsubscribed"),
                ).where(e.campaign_id == campaign_id)
            )
        )
        .mappings()
        .one()
    )
    if meta is None and not calls["attempted"] and not emails["messages"]:
        return None
    attempted, ended = int(calls["attempted"]), int(calls["ended"])
    return {
        "campaign_id": campaign_id,
        "channel": meta["channel"] if meta else None,
        "launched_at": meta["launched_at"] if meta else None,
        "calls": {
            "attempted": attempted,
            "answered": int(calls["answered"]),
            "ended": ended,
            "completed": int(calls["completed"]),
            "failed": int(calls["failed"]),
            "opted_out": int(calls["opted_out"]),
            "qualified": int(calls["qualified"]),
            "answer_rate": ratio(int(calls["answered"]), attempted),
            "average_duration_seconds": per(int(calls["duration"]), ended),
            "cost_micros": int(calls["cost"]),
            "cost_per_call_micros": per(int(calls["cost"]), ended),
        },
        "emails": {
            "messages": int(emails["messages"]),
            "accepted": int(emails["accepted"]),
            "delivered": int(emails["delivered"]),
            "bounced": int(emails["bounced"]),
            "complained": int(emails["complained"]),
            "unsubscribed": int(emails["unsubscribed"]),
            "delivery_rate": ratio(int(emails["delivered"]), int(emails["accepted"])),
        },
    }

"""Call lifecycle (build.md §3.1G): queued, scheduled, initiating, ringing, connected, in_progress, completed,
failed, canceled, opted_out.

Provider callbacks arrive duplicated and out of order. A transition is applied only when it is legal from the
current state, so a late `ringing` after `completed` is ignored rather than moving the call backwards.
"""

from __future__ import annotations

from typing import Literal

State = Literal[
    "queued",
    "scheduled",
    "initiating",
    "ringing",
    "connected",
    "in_progress",
    "completed",
    "failed",
    "canceled",
    "opted_out",
]
FinalState = Literal["completed", "no_answer", "busy", "failed", "opted_out", "canceled"]

TERMINAL: frozenset[str] = frozenset({"completed", "failed", "canceled", "opted_out"})
LIVE: frozenset[str] = frozenset({"initiating", "ringing", "connected", "in_progress"})
PENDING: frozenset[str] = frozenset({"queued", "scheduled"})
ANSWERED: frozenset[str] = frozenset({"connected", "in_progress"})

TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"scheduled", "initiating", "canceled", "failed"}),
    "scheduled": frozenset({"queued", "canceled", "failed"}),
    # initiating -> queued only by reconciliation, when the provider confirms it has no such call.
    "initiating": frozenset(
        {"queued", "scheduled", "ringing", "connected", "in_progress", "completed", "failed", "canceled"}
    ),
    "ringing": frozenset({"connected", "in_progress", "completed", "failed", "canceled"}),
    "connected": frozenset({"in_progress", "completed", "failed", "opted_out"}),
    "in_progress": frozenset({"completed", "failed", "opted_out"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "canceled": frozenset(),
    "opted_out": frozenset(),
}


class IllegalTransition(Exception):
    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"{current} -> {target}")
        self.current, self.target = current, target


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def check_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise IllegalTransition(current, target)


# Twilio CallStatus -> (target state, end reason).
PROVIDER_STATUS: dict[str, tuple[str, str | None]] = {
    "queued": ("initiating", None),
    "initiated": ("initiating", None),
    "ringing": ("ringing", None),
    "in-progress": ("connected", None),
    "answered": ("connected", None),
    "completed": ("completed", "completed"),
    "busy": ("failed", "busy"),
    "no-answer": ("failed", "no_answer"),
    "failed": ("failed", "provider_failed"),
    "canceled": ("canceled", "provider_canceled"),
}


def target_for_provider_status(status: str) -> tuple[str, str | None] | None:
    return PROVIDER_STATUS.get(status)


def final_state(state: str, end_reason: str | None, *, opted_out: bool) -> FinalState:
    """The `call.completed` contract (campaign-service): completed|no_answer|busy|failed|opted_out|canceled."""
    if opted_out or state == "opted_out":
        return "opted_out"
    if end_reason == "answering_machine":
        return "no_answer"
    if state == "completed":
        return "completed"
    if state == "canceled":
        return "canceled"
    if end_reason == "no_answer":
        return "no_answer"
    if end_reason == "busy":
        return "busy"
    return "failed"

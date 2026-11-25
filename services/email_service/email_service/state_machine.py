"""Campaign and message state machines (build.md §4.1). Pure functions, unit-tested.

Campaign:  draft -> scheduled -> running <-> paused -> completed | canceled | failed
Message:   pending -> queued -> sending -> accepted -> delivered | bounced | complained | rejected | unsubscribed
           | failed. Provider acceptance is not inbox delivery: `accepted` only means SES took the message.
"""

from __future__ import annotations


class InvalidTransition(ValueError):
    def __init__(self, kind: str, current: str, target: str) -> None:
        super().__init__(f"{kind} cannot go from {current} to {target}")
        self.current, self.target = current, target


CAMPAIGN_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"scheduled", "canceled"}),
    "scheduled": frozenset({"running", "paused", "canceled", "failed"}),
    "running": frozenset({"paused", "completed", "canceled", "failed"}),
    # Resuming a campaign that was paused before it started returns it to `scheduled`.
    "paused": frozenset({"running", "scheduled", "canceled", "failed"}),
    "completed": frozenset(),
    "canceled": frozenset(),
    "failed": frozenset(),
}
CAMPAIGN_LIVE = frozenset({"scheduled", "running", "paused"})
CAMPAIGN_TERMINAL = frozenset({"completed", "canceled", "failed"})

# Actions exposed by the API -> target state.
CAMPAIGN_ACTIONS = {"schedule": "scheduled", "pause": "paused", "resume": "running", "cancel": "canceled"}


def campaign_next(current: str, target: str) -> str:
    if target not in CAMPAIGN_TRANSITIONS.get(current, frozenset()):
        raise InvalidTransition("campaign", current, target)
    return target


MESSAGE_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"queued", "failed"}),
    "queued": frozenset({"sending", "failed"}),
    # A provider event can confirm a send before the worker records `accepted` (or after an ambiguous error).
    "sending": frozenset(
        {"queued", "accepted", "delivered", "bounced", "complained", "rejected", "failed", "unsubscribed"}
    ),
    "accepted": frozenset({"delivered", "bounced", "complained", "rejected", "unsubscribed", "failed"}),
    # Asynchronous bounces and complaints can follow a delivery.
    "delivered": frozenset({"bounced", "complained", "unsubscribed"}),
    "bounced": frozenset({"complained"}),
    "complained": frozenset(),
    "rejected": frozenset(),
    "unsubscribed": frozenset({"complained"}),
    "failed": frozenset(),
}
MESSAGE_FINAL = frozenset({"delivered", "bounced", "complained", "rejected", "unsubscribed", "failed"})
MESSAGE_OPEN = frozenset({"pending", "queued", "sending"})
SENT_STATES = ("accepted", "delivered", "bounced", "complained", "rejected", "unsubscribed")


def message_can(current: str, target: str) -> bool:
    return target in MESSAGE_TRANSITIONS.get(current, frozenset())


def message_next(current: str, target: str) -> str:
    if not message_can(current, target):
        raise InvalidTransition("message", current, target)
    return target


def sources_for(target: str) -> tuple[str, ...]:
    """States from which `target` is reachable: used as a guard in `UPDATE ... WHERE state IN (...)`."""
    return tuple(sorted(s for s, nxt in MESSAGE_TRANSITIONS.items() if target in nxt))

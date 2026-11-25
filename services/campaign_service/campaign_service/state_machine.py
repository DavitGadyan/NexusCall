"""Call campaign lifecycle: draft → scheduled → running ⇄ paused → completed | canceled | failed.

Transitions are applied with compare-and-swap on (state, version), so two concurrent actions can never both
succeed (see `domain.transition`)."""

from __future__ import annotations

TRANSITIONS: dict[tuple[str, str], str] = {
    ("draft", "schedule"): "scheduled",
    ("draft", "cancel"): "canceled",
    ("scheduled", "unschedule"): "draft",
    ("scheduled", "launch"): "running",
    ("scheduled", "cancel"): "canceled",
    ("running", "pause"): "paused",
    ("running", "complete"): "completed",
    ("running", "cancel"): "canceled",
    ("running", "fail"): "failed",
    ("paused", "resume"): "running",
    ("paused", "cancel"): "canceled",
    ("paused", "fail"): "failed",
}
TERMINAL = frozenset({"completed", "canceled", "failed"})
LIVE = frozenset({"running", "paused"})  # tracked in the dispatch registry
EVENTS = {
    "launch": "campaign.launched",
    "pause": "campaign.paused",
    "resume": "campaign.resumed",
    "cancel": "campaign.canceled",
}


class InvalidTransition(ValueError):
    def __init__(self, state: str, action: str) -> None:
        super().__init__(f"Cannot {action} a campaign that is {state}")
        self.state, self.action = state, action


def next_state(state: str, action: str) -> str:
    try:
        return TRANSITIONS[(state, action)]
    except KeyError as exc:
        raise InvalidTransition(state, action) from exc


def allowed_actions(state: str) -> list[str]:
    return sorted(action for (s, action) in TRANSITIONS if s == state)

"""LOCAL DEVELOPMENT showcase scenario for the ACME demo tenant ("Acme Home Services"): a month of a busy AI
call center, used for screenshots and demo videos.

One pure, deterministic generator builds the whole story (contacts, call campaigns, calls with transcripts,
bookings, email campaigns, inbox threads with AI drafts, and the analytics read model derived from all of it),
so every service's `seed_showcase.py` writes its own slice and the numbers agree across pages.

* Deterministic: fixed ids (`sid`), a seeded `random.Random`, and every timestamp relative to one anchor date.
  The anchor is the UTC date of the first run; it is persisted implicitly (see `anchor_marker`), so later runs
  rebuild exactly the same scenario and change nothing.
* Clearly fictional: common generic names, emails under `example.com`, phones only in the reserved 555-01xx
  range, no real companies or people.
* Inert: every call, email, campaign message and draft is in a terminal or human-review state, and live
  campaigns are not put in any dispatcher/scheduler index, so seeding never makes the stack dial or send.
"""

from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from nexus_core.devseed import ACME, DEMO_USERS, demo_id

TENANT = ACME
TZ = ZoneInfo(ACME.timezone)
WINDOW_DAYS = 45  # analytics window: anchor - 44 .. anchor (keeps any "last 30 days" chart full)
SEED = 20_261_126

MAIN_LINE = "+12125550100"  # call_service.seed inbound number
HANDOFF_LINE = "+12125550198"
INBOUND_NUMBER_ID = demo_id("inbound_number", 1)
VOICE_AGENT_ID = demo_id("agent", 1)
EMAIL_AGENT_ID = demo_id("agent", 2)
MAILBOX_ID = demo_id("mailbox", 1)
MAILBOX_ADDRESS = "support@acme.example.test"
KNOWLEDGE_SOURCE_ID = demo_id("knowledge_source", 1)
MAIN_RESOURCE_ID = demo_id("booking_resource", 1)
EMAIL_SENDER_ID = demo_id("email_sender", 1)
EMAIL_FROM = "news@acme.test"
EMAIL_REPLY_TO = "support@acme.test"

_USERS = {u.email: u for u in DEMO_USERS}
CAMPAIGN_MANAGER = _USERS["campaigns@acme.test"].id
EMAIL_REVIEWER = _USERS["email@acme.test"].id
CALL_AGENT_USER = _USERS["agent@acme.test"].id

# Contact n=1 is created exactly this long before the anchor date, at 15:00 UTC: reading it back recovers the anchor.
_MARKER_DAYS = 400


def sid(kind: str, n: int) -> uuid.UUID:
    """Showcase ids live in their own namespace, so they never collide with `seed.py` ids."""
    return demo_id(f"showcase:{kind}", n)


ANCHOR_CONTACT_ID = sid("contact", 1)


def anchor_marker(anchor: date) -> datetime:
    return datetime.combine(anchor - timedelta(days=_MARKER_DAYS), time(15, 0), UTC)


def anchor_from_marker(created_at: datetime) -> date:
    return created_at.astimezone(UTC).date() + timedelta(days=_MARKER_DAYS)


# ===================================================================================================== model
@dataclass(slots=True)
class Contact:
    n: int
    id: uuid.UUID
    first: str
    last: str
    email: str
    phone: str
    created_at: datetime
    source: str
    tags: list[str]
    new_lead: bool
    lead_id: uuid.UUID
    lead_status: str
    lead_score: int
    lead_source: str
    lead_qualified: bool | None = None
    last_outcome: str | None = None
    lead_call_id: uuid.UUID | None = None
    lead_campaign_id: uuid.UUID | None = None
    lead_booking_id: uuid.UUID | None = None
    lead_updated_at: datetime | None = None
    phone_opt_out: tuple[datetime, uuid.UUID] | None = None  # (when, call id)
    email_suppressed: tuple[str, datetime, uuid.UUID] | None = None  # (reason, when, message id)

    @property
    def name(self) -> str:
        return f"{self.first} {self.last}"


@dataclass(slots=True)
class Segment:
    seq: int
    role: str
    text: str
    started_ms: int
    interrupted: bool


@dataclass(slots=True)
class Booking:
    n: int
    id: uuid.UUID
    resource: str  # key of RESOURCES
    contact: Contact
    service: str
    start: datetime
    end: datetime
    status: str
    source_type: str
    source_id: str | None
    created_by: str
    created_at: datetime
    confirmed_at: datetime | None = None
    canceled_at: datetime | None = None
    cancel_reason: str | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class Call:
    n: int
    id: uuid.UUID
    direction: str
    contact: Contact
    topic: str
    created_at: datetime
    campaign: CallCampaign | None = None
    member: CampaignMember | None = None
    attempt: int = 1
    initiated_at: datetime | None = None
    started_at: datetime | None = None
    answered_at: datetime | None = None
    ended_at: datetime | None = None
    duration: int = 0
    state: str = "completed"
    end_reason: str | None = None
    provider_status: str = "completed"
    outcome: str | None = None
    qualified: bool | None = None
    handoff_state: str | None = None
    handoff_at: datetime | None = None
    opted_out: bool = False
    cost_micros: int = 0
    segments: list[Segment] = field(default_factory=list)
    voice_end_reason: str | None = None
    voice_ended_at: datetime | None = None
    barge_ins: int = 0
    booking: Booking | None = None

    @property
    def has_session(self) -> bool:
        return bool(self.segments)

    @property
    def turns(self) -> int:
        return sum(1 for s in self.segments if s.role == "caller")

    @property
    def final_state(self) -> str:
        """call_service.state_machine.final_state."""
        if self.opted_out or self.state == "opted_out":
            return "opted_out"
        if self.end_reason == "answering_machine":
            return "no_answer"
        if self.state in ("completed", "canceled"):
            return self.state
        if self.end_reason in ("no_answer", "busy"):
            return self.end_reason
        return "failed"


@dataclass(slots=True)
class CampaignMember:
    n: int
    id: uuid.UUID
    contact: Contact
    created_at: datetime
    state: str = "pending"
    attempts: int = 0
    next_attempt_at: datetime | None = None
    last_call_id: uuid.UUID | None = None
    outcome: str | None = None
    skip_reason: str | None = None
    dispatched_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class CampaignEvent:
    id: uuid.UUID
    kind: str
    actor: str
    created_at: datetime
    from_state: str | None = None
    to_state: str | None = None
    contact_id: uuid.UUID | None = None
    call_id: uuid.UUID | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CallCampaign:
    n: int
    id: uuid.UUID
    key: str
    name: str
    description: str
    topic: str
    state: str
    segment_id: uuid.UUID
    created_at: datetime
    window_start: str = "10:00"
    window_end: str = "19:00"
    window_days: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri")
    max_attempts: int = 2
    retry_delay_seconds: int = 86_400
    calls_per_minute: int = 6
    max_concurrency: int = 3
    start_at: datetime | None = None
    end_at: datetime | None = None
    launched_at: datetime | None = None
    completed_at: datetime | None = None
    paused_at: datetime | None = None
    snapshot_status: str = "none"
    snapshot_total: int = 0
    members: list[CampaignMember] = field(default_factory=list)
    events: list[CampaignEvent] = field(default_factory=list)
    updated_at: datetime | None = None
    version: int = 1


@dataclass(slots=True)
class EmailMessage:
    n: int
    id: uuid.UUID
    contact: Contact
    state: str
    queued_at: datetime
    accepted_at: datetime
    delivered_at: datetime | None
    state_changed_at: datetime
    suppression_reason: str | None = None


@dataclass(slots=True)
class EmailCampaign:
    n: int
    id: uuid.UUID
    key: str
    name: str
    template_key: str
    segment_id: uuid.UUID
    state: str
    created_at: datetime
    launched_at: datetime
    completed_at: datetime | None
    max_per_minute: int = 30
    messages: list[EmailMessage] = field(default_factory=list)


@dataclass(slots=True)
class InboxMessage:
    id: uuid.UUID
    direction: str
    from_address: str
    from_name: str
    to_address: str
    subject: str
    text: str
    received_at: datetime
    rfc_message_id: str | None
    in_reply_to: str | None = None
    draft_version: int | None = None


@dataclass(slots=True)
class DraftVersion:
    version: int
    body: str
    author_type: str
    author_id: str
    created_at: datetime
    quality: dict[str, Any]
    citations: list[dict[str, str]]


@dataclass(slots=True)
class DraftAction:
    id: uuid.UUID
    action: str
    version: int | None
    actor_type: str
    actor_id: str
    created_at: datetime
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Thread:
    n: int
    id: uuid.UUID
    contact: Contact
    scenario: str
    subject: str
    intent: str
    priority: str
    status: str
    sensitive: bool
    messages: list[InboxMessage]
    assigned_to: str | None = None
    draft_id: uuid.UUID | None = None
    draft_status: str | None = None
    draft_summary: str = ""
    draft_versions: list[DraftVersion] = field(default_factory=list)
    draft_actions: list[DraftAction] = field(default_factory=list)
    draft_created_at: datetime | None = None
    draft_updated_at: datetime | None = None
    approved_version: int | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    sent_at: datetime | None = None
    escalated: bool = False
    escalation_reason: str | None = None
    reply_email_id: uuid.UUID | None = None

    @property
    def last_message_at(self) -> datetime:
        return max(m.received_at for m in self.messages)


@dataclass(slots=True)
class TxEmail:
    """A transactional or reply email sent through email-service (booking confirmations, approved AI replies)."""

    n: int
    id: uuid.UUID
    category: str  # transactional | reply
    contact: Contact
    from_address: str
    subject: str
    body: str
    accepted_at: datetime
    delivered_at: datetime
    source_type: str
    source_id: str


@dataclass(slots=True)
class Scenario:
    anchor: date
    contacts: list[Contact]
    call_campaigns: list[CallCampaign]
    calls: list[Call]
    bookings: list[Booking]
    email_campaigns: list[EmailCampaign]
    threads: list[Thread]
    daily_metrics: dict[tuple[date, str, str], int]
    tx_emails: list[TxEmail] = field(default_factory=list)

    @property
    def window(self) -> list[date]:
        return [self.anchor - timedelta(days=WINDOW_DAYS - 1 - i) for i in range(WINDOW_DAYS)]


# ================================================================================================ reference data
TAGS: dict[str, tuple[str, str]] = {
    "hvac": ("Heating & cooling customer", "#d97706"),
    "plumbing": ("Plumbing customer", "#2563eb"),
    "club": ("Comfort Club member", "#059669"),
    "water_heater": ("Water heater 10+ years", "#7c3aed"),
    "new": ("New lead", "#db2777"),
}
# key -> (n, name, description, tag key)
SEGMENTS: dict[str, tuple[int, str, str, str]] = {
    "hvac": (1, "Past heating & cooling customers", "Contacts tagged as heating & cooling customers.", "hvac"),
    "club": (2, "Comfort Club members", "Members of the annual maintenance plan.", "club"),
    "water_heater": (3, "Water heaters 10+ years old", "Customers whose water heater is over ten years old.",
                     "water_heater"),  # fmt: skip
    "plumbing": (4, "Past plumbing customers", "Contacts tagged as plumbing customers.", "plumbing"),
}
ALL_CONTACTS_SEGMENT = demo_id("segment", 1)  # crm_service.seed "All contactable leads"


def segment_id(key: str) -> uuid.UUID:
    return sid("segment", SEGMENTS[key][0])


# key -> (id, name, kind, open hour, close hour); "main" is the existing seed.py calendar (weekdays 09-17).
RESOURCES: dict[str, tuple[uuid.UUID, str, str, int, int]] = {
    "main": (MAIN_RESOURCE_ID, "Main calendar", "calendar", 9, 17),
    "hvac": (sid("resource", 1), "Heating & cooling crew", "staff", 8, 17),
    "plumbing": (sid("resource", 2), "Plumbing crew", "staff", 8, 17),
}

FIRST_NAMES = (
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda", "David", "Elizabeth",
    "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica", "Thomas", "Sarah", "Charles", "Karen",
    "Christopher", "Lisa", "Daniel", "Nancy", "Matthew", "Betty", "Anthony", "Sandra", "Mark", "Margaret",
    "Donald", "Ashley", "Steven", "Kimberly", "Andrew", "Emily", "Paul", "Donna", "Joshua", "Michelle",
    "Kenneth", "Carol", "Kevin", "Amanda", "Brian", "Melissa", "George", "Deborah", "Timothy", "Stephanie",
    "Ronald", "Rebecca", "Jason", "Laura", "Edward", "Helen", "Jeffrey", "Sharon", "Ryan", "Cynthia",
    "Gary", "Kathleen", "Nicholas", "Amy", "Eric", "Angela", "Jonathan", "Anna", "Larry", "Brenda",
    "Justin", "Pamela", "Scott", "Nicole", "Brandon", "Samantha", "Gregory", "Katherine", "Samuel", "Christine",
)  # fmt: skip
LAST_NAMES = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Miller", "Davis", "Wilson", "Anderson", "Taylor",
    "Thomas", "Moore", "Martin", "Jackson", "Thompson", "White", "Harris", "Clark", "Lewis", "Robinson",
    "Walker", "Young", "Allen", "King", "Wright", "Scott", "Hill", "Green", "Adams", "Baker",
    "Nelson", "Carter", "Mitchell", "Roberts", "Turner", "Phillips", "Campbell", "Parker", "Evans", "Edwards",
    "Collins", "Stewart", "Morris", "Murphy", "Cook", "Rogers", "Morgan", "Cooper", "Peterson", "Reed",
    "Bailey", "Bell", "Kelly", "Howard", "Ward", "Cox", "Richardson", "Wood", "Watson", "Brooks",
    "Bennett", "Gray", "Hughes", "Price", "Sanders", "Myers", "Long", "Ross", "Foster", "Powell",
)  # fmt: skip
# NYC-metro area codes; every number is +1 <area> 555-01xx (reserved for fiction). 212 is used by seed.py.
AREA_CODES = ("646", "718", "917", "347", "516", "914", "201", "929")
N_PAST, N_NEW = 120, 40

# Topic -> (service label for bookings, resource, minutes)
SERVICES: dict[str, tuple[str, str, int]] = {
    "ac_tuneup": ("End-of-season AC check", "hvac", 60),
    "furnace_tuneup": ("Furnace tune-up", "hvac", 60),
    "club_renewal": ("Comfort Club maintenance visit", "hvac", 90),
    "water_heater": ("Water heater replacement estimate", "main", 60),
    "furnace_safety": ("Furnace safety check", "hvac", 60),
    "no_heat": ("No-heat repair visit", "hvac", 90),
    "water_heater_leak": ("Water heater leak repair", "plumbing", 120),
    "drain": ("Drain cleaning", "plumbing", 60),
    "boiler_service": ("Annual boiler service", "hvac", 90),
    "estimate": ("In-home estimate", "main", 60),
}


# ================================================================================================ time helpers
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def is_holiday(d: date) -> bool:
    return (d.month, d.day) in ((1, 1), (7, 4), (12, 25)) or d in (
        _nth_weekday(d.year, 11, 3, 4),  # Thanksgiving
        _nth_weekday(d.year, 9, 0, 1),  # Labor Day
        _last_weekday(d.year, 5, 0),  # Memorial Day
    )


def is_workday(d: date) -> bool:
    return d.weekday() < 5 and not is_holiday(d)


def local(d: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime.combine(d, time(hour, minute, second), TZ).astimezone(UTC)


def local_date(ts: datetime) -> date:
    return ts.astimezone(TZ).date()


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def slot_phrase(start: datetime, ref: datetime) -> str:
    """'tomorrow at 9 a.m.', 'Tuesday at 2 p.m.' or 'Tuesday the 8th at 2 p.m.' (local time)."""
    s, r = start.astimezone(TZ), ref.astimezone(TZ)
    hour = s.hour % 12 or 12
    minutes = f":{s.minute:02d}" if s.minute else ""
    clock = f"{hour}{minutes} {'a.m.' if s.hour < 12 else 'p.m.'}"
    delta = (s.date() - r.date()).days
    if delta == 1:
        return f"tomorrow at {clock}"
    if delta < 7:
        return f"{s.strftime('%A')} at {clock}"
    return f"{s.strftime('%A')} the {_ordinal(s.day)} at {clock}"


def _workdays(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1) if is_workday(start + timedelta(days=i))]


def _next_workday(d: date) -> date:
    d += timedelta(days=1)
    while not is_workday(d):
        d += timedelta(days=1)
    return d


def _workday_on_or_before(d: date) -> date:
    while not is_workday(d):
        d -= timedelta(days=1)
    return d


# ================================================================================================ transcripts
OUTBOUND_PITCH: dict[str, tuple[str, str]] = {
    # topic -> (pitch, short subject for voicemail)
    "ac_tuneup": (
        "Now that the summer heat is over, it's a great time to have your air conditioner cleaned and checked "
        "before you cover it for the winter. We have openings over the next two weeks. Shall I find you a time?",
        "an end-of-season AC check",
    ),
    "furnace_tuneup": (
        "We're calling our heating customers before the cold weather sets in. It's been about a year since your "
        "last furnace service, and we have tune-up openings over the next couple of weeks. Would you like me to "
        "find you a time?",
        "a furnace tune-up before winter",
    ),
    "club_renewal": (
        "Your Comfort Club maintenance plan is up for renewal this month. It covers your yearly heating and "
        "cooling visits and priority scheduling. Would you like to renew and book your winter visit?",
        "your Comfort Club renewal",
    ),
    "water_heater": (
        "Our records show your water heater is more than ten years old. We're offering free in-home estimates "
        "for replacements this month, no obligation. Would you like one of our estimators to stop by?",
        "a free water heater estimate",
    ),
    "furnace_safety": (
        "With the first cold nights here, we're offering a quick furnace safety check: burners, heat exchanger, "
        "carbon monoxide test and a fresh filter. Would that be helpful for you this season?",
        "a winter furnace safety check",
    ),
}
CALLER_ACK = ("Yeah, sure.", "Okay, go ahead.", "I've got a minute, what's this about?", "Sure, what's up?",
              "Uh, yes, this is them.")  # fmt: skip
CALLER_YES = {
    "ac_tuneup": (
        "Sure, it was struggling a bit in August.",
        "Yeah, let's get it looked at before winter.",
    ),
    "furnace_tuneup": (
        "Actually, good timing. It's been making a rattling noise when it starts up.",
        "Yes, let's do that. We skipped it last year.",
        "Sure, I'd rather do it before it gets really cold.",
    ),
    "club_renewal": (
        "Yes, we want to keep it. Go ahead and book the visit.",
        "Sure, the plan has been worth it for us.",
    ),
    "water_heater": (
        "Yeah, it's been running out of hot water lately, so that would help.",
        "Sure, I'd like to know what a new one would cost.",
    ),
    "furnace_safety": (
        "That sounds smart, we have a baby in the house now.",
        "Yes please, the furnace is pretty old.",
    ),
}
CALLER_NO = (
    "We just had someone out last month, so we're all set, thanks.",
    "Not right now, thanks. Money's a bit tight before the holidays.",
    "No thanks, my brother-in-law takes care of that for us.",
    "We're actually selling the house, so we'll pass.",
)
CALLBACK_ASK = (
    "I'm at work right now. Can someone call me back after five?",
    "Can you call me back tomorrow morning? I'm driving.",
    "This isn't a great time. Could someone call me back later this week?",
)
INTEREST_ASK = (
    "How much does that usually run?",
    "What does it cost, roughly?",
)
INTEREST_PRICE = {
    "ac_tuneup": "The AC check is a standard visit: $89, including the first 30 minutes of labor.",
    "furnace_tuneup": "A tune-up is part of a standard visit: the call-out is $89 and includes the first 30 minutes "
    "of labor.",
    "club_renewal": "The plan renews at the same price as last year, and members get priority booking and no "
    "call-out fee.",
    "water_heater": "The estimate itself is free. Installation quotes depend on the size and type of heater.",
    "furnace_safety": "The safety check is a standard visit: $89, including the first 30 minutes of labor.",
}
INTEREST_DEFER = (
    "Okay. Let me talk it over with my wife and I'll call you back.",
    "Good to know. I'll check my schedule and give you a call.",
    "Alright, send me the details by email and I'll think about it.",
)


def _seg_list(lines: list[tuple[str, str]], rng: random.Random) -> tuple[list[Segment], int, int]:
    """(segments, total ms, barge-ins) with realistic speech timing (~16 characters a second)."""
    out: list[Segment] = []
    t = rng.randint(250, 600)
    barge = 0
    for seq, (role, text) in enumerate(lines):
        dur = 450 + len(text) * 62
        interrupted = role == "agent" and len(text) > 140 and rng.random() < 0.15
        if interrupted:
            barge += 1
            dur = int(dur * rng.uniform(0.55, 0.8))
        out.append(Segment(seq, role, text, t, interrupted))
        t += dur + rng.randint(350, 1300)
    return out, t, barge


def _outbound_lines(topic: str, outcome: str, c: Contact, call_at: datetime, booking: Booking | None,
                    alt_slot: str, rng: random.Random) -> list[tuple[str, str]]:  # fmt: skip
    pitch, _ = OUTBOUND_PITCH[topic]
    a: list[tuple[str, str]] = [
        ("agent", f"Hi, this is the virtual assistant calling from Acme Home Services. I'm an automated AI "
                  f"assistant. Am I speaking with {c.first}?"),  # fmt: skip
    ]
    if outcome == "wrong_number":
        a += [
            ("caller", f"No, I think you have the wrong number. There's no {c.first} here."),
            ("agent", "I'm sorry for the trouble. I'll make a note so we don't call this number again for them. "
                      "Have a good day."),  # fmt: skip
            ("caller", "No problem, bye."),
        ]
        return a
    a += [("caller", rng.choice(CALLER_ACK)), ("agent", f"Thanks, {c.first}. {pitch}")]
    if outcome == "appointment_requested":
        assert booking is not None
        slot = slot_phrase(booking.start, call_at)
        a += [
            ("caller", rng.choice(CALLER_YES[topic])),
            ("agent", f"Great. I have {alt_slot} or {slot}. Which works better for you?"),
            ("caller", f"{slot.split(' at ')[0].capitalize()} works."),
            ("agent", f"You're all set for {slot}. A technician will call about 30 minutes before arriving. Is the "
                      "address we have on file still correct?"),  # fmt: skip
            ("caller", rng.choice(("Yes, same address.", "Yep, nothing's changed.", "Yes, that's right."))),
            ("agent", "Perfect. You'll get a confirmation email shortly. Is there anything else I can help with?"),
            ("caller", rng.choice(("No, that's it. Thank you.", "Nope, that's everything. Thanks!"))),
            ("agent", f"Thank you, {c.first}. Have a great day!"),
        ]
    elif outcome == "callback_requested":
        ask = rng.choice(CALLBACK_ASK)
        when = "after five" if "five" in ask else "tomorrow morning" if "morning" in ask else "later this week"
        a += [
            ("caller", ask),
            ("agent", f"Of course. I'll ask a member of our team to call you back {when}. Is this the best number "
                      "to reach you?"),  # fmt: skip
            ("caller", "Yes, this number is fine."),
            ("agent", "Got it. Thanks for your time, and talk soon."),
        ]
    elif outcome == "interested":
        a += [
            ("caller", rng.choice(INTEREST_ASK)),
            ("agent", INTEREST_PRICE[topic]),
            ("caller", rng.choice(INTEREST_DEFER)),
            ("agent", "No problem at all. I'll email you the details, and you can reply or call us at 212-555-0100 "
                      "whenever you're ready."),  # fmt: skip
            ("caller", "Sounds good, thanks."),
        ]
    elif outcome == "not_interested":
        a += [
            ("caller", rng.choice(CALLER_NO)),
            ("agent", "Understood, thanks for letting me know. If anything comes up, we're just a call away. Have a "
                      "good day."),  # fmt: skip
            ("caller", "You too, bye."),
        ]
    elif outcome == "opted_out":
        a += [
            ("caller", "Please take me off your call list. I don't want these calls."),
            ("agent", "Understood. I've removed this number from our call list and you won't receive further calls "
                      "from us. Sorry for the interruption."),  # fmt: skip
        ]
    elif outcome == "escalated":
        a += [
            ("caller", "Actually, before that, I have a question about the bill from my last visit. Can I talk to a "
                       "person?"),  # fmt: skip
            ("agent", "Of course. I'll connect you with a member of our office team now. One moment, please."),
        ]
    else:  # other
        a += [
            ("caller", "Sorry, you're breaking up. Can you try another time?"),
            ("agent", "Of course, sorry about that. We'll try again another day. Goodbye."),
        ]
    return a


# Inbound scenarios: topic -> (outcome, weight)
INBOUND_TOPICS: dict[str, tuple[str, float]] = {
    "no_heat": ("appointment_requested", 0.15),
    "water_heater_leak": ("appointment_requested", 0.10),
    "drain": ("appointment_requested", 0.10),
    "boiler_service": ("appointment_requested", 0.08),
    "pricing": ("interested", 0.14),
    "hours": ("other", 0.11),
    "billing": ("escalated", 0.10),
    "estimate_callback": ("callback_requested", 0.14),
    "wrong_number": ("wrong_number", 0.04),
    "not_now": ("not_interested", 0.04),
}
GREETING = (
    "Thanks for calling Acme Home Services. I'm the virtual assistant, an AI, and I can help with appointments, "
    "prices and questions. How can I help you today?"
)


def _inbound_lines(topic: str, c: Contact, call_at: datetime, booking: Booking | None,
                   rng: random.Random) -> list[tuple[str, str]]:  # fmt: skip
    a: list[tuple[str, str]] = [("agent", GREETING)]
    slot = slot_phrase(booking.start, call_at) if booking else ""
    close = ("caller", rng.choice(("Great, thank you.", "Perfect, thanks so much.", "Okay, thanks for your help.")))
    confirm_name = [
        ("agent", "Can I have the name on the account, please?"),
        ("caller", f"It's {c.first} {c.last}."),
    ]
    if topic == "no_heat":
        a += [
            ("caller", "Hi, our furnace stopped working last night. It's just blowing cold air."),
            ("agent", "I'm sorry to hear that. First, do you smell gas anywhere in the house?"),
            ("caller", "No, no gas smell. Just no heat."),
            ("agent", "Okay, good. If you ever do smell gas, leave the house and call your gas company's emergency "
                      f"line first. I can have a heating technician out {slot}. Does that work?"),  # fmt: skip
            ("caller", "Yes, please. The sooner the better."),
            *confirm_name,
            ("agent", f"Thanks, {c.first}. You're booked for {slot}. The standard call-out visit is $89 and includes "
                      "the first 30 minutes of labor. You'll get a confirmation email in a minute."),  # fmt: skip
            close,
            ("agent", "You're welcome. Stay warm, and we'll see you soon."),
        ]
    elif topic == "water_heater_leak":
        a += [
            ("caller", "There's water pooling under my water heater in the basement. Not a lot, but it's new."),
            ("agent", "Thanks for catching that early. If you can, turn the cold-water valve above the tank to off. "
                      f"I can get a plumber to you {slot}. Would that work?"),  # fmt: skip
            ("caller", "Yes, that works. I'll be home."),
            *confirm_name,
            ("agent", f"Got it, {c.first}. You're booked for {slot} with our plumbing crew. You'll get a "
                      "confirmation by email."),  # fmt: skip
            close,
        ]
    elif topic == "drain":
        a += [
            ("caller", "My kitchen sink is completely clogged. I tried a plunger and it didn't do anything."),
            ("agent", f"That sounds frustrating. Drain cleaning is something we do every day. I have an opening "
                      f"{slot}. Shall I book it?"),  # fmt: skip
            ("caller", "Yes, please book it."),
            *confirm_name,
            ("agent", f"Done. You're booked for {slot}. Please avoid chemical drain cleaners before we arrive, they "
                      "can be hazardous for the technician."),  # fmt: skip
            close,
        ]
    elif topic == "boiler_service":
        a += [
            ("caller", "Hi, I'd like to book the yearly service for my boiler."),
            ("agent", f"Happy to help. Annual boiler servicing is $129. The next opening is {slot}. Does that suit "
                      "you?"),  # fmt: skip
            ("caller", "That's fine."),
            *confirm_name,
            ("agent", f"Thanks, {c.first}. You're booked for {slot}. We'll send a confirmation by email."),
            close,
        ]
    elif topic == "pricing":
        a += [
            ("caller", "How much do you charge just to come out and look at a leaky faucet?"),
            ("agent", "A standard call-out visit is $89 and includes the first 30 minutes of labor. Additional labor "
                      "is $45 per 30 minutes. Most faucet repairs fit in the first half hour."),  # fmt: skip
            ("caller", "Okay, that's reasonable. Let me check my schedule and I'll call back to book."),
            ("agent", "Sounds good. You can also reply to any of our emails or book online. Anything else?"),
            ("caller", "No, that's all. Thanks."),
        ]
    elif topic == "hours":
        a += [
            ("caller", "Are you guys open on Saturdays?"),
            ("agent", "Yes, we're open Saturday from 9 a.m. to 1 p.m., and Monday to Friday from 8 a.m. to 6 p.m. "
                      "We're closed on Sundays and public holidays."),  # fmt: skip
            ("caller", "Great, that's all I needed."),
            ("agent", "Happy to help. Have a good day!"),
        ]
    elif topic == "billing":
        a += [
            ("caller", "I think I was charged twice for the visit last week. I'd like to talk to someone about it."),
            ("agent", "I'm sorry about that. Billing questions are best handled by our office team, so let me "
                      "transfer you to a person now. One moment, please."),  # fmt: skip
        ]
    elif topic == "estimate_callback":
        a += [
            ("caller", "My water heater is about twelve years old and I'm thinking about replacing it. Can someone "
                       "call me with a quote?"),  # fmt: skip
            ("agent", "Of course. Quotes for installations are free. I'll ask one of our estimators to call you "
                      "back. What time of day is best?"),  # fmt: skip
            ("caller", "Mornings are best, before ten."),
            ("agent", f"Noted, {c.first}. Someone will call you tomorrow morning before ten."),
            ("caller", "Perfect, thank you."),
        ]
    elif topic == "wrong_number":
        a += [
            ("caller", "Oh, sorry, is this the pharmacy? I think I dialed the wrong number."),
            ("agent", "No problem. This is Acme Home Services, a plumbing and heating company. Have a nice day!"),
        ]
    else:  # not_now
        a += [
            ("caller", "I got a voicemail from you about a tune-up. We're fine for now, thanks."),
            ("agent", "Thanks for calling back to let us know. We'll be here if you need us. Have a great day."),
        ]
    return a


# ================================================================================================ inbox scenarios
SIGN_OFF = "\n\nBest regards,\nThe Acme Home Services team"
# key -> (subject, intent, priority, sensitive, inbound text, AI draft text (or None), summary, flags, confidence)
INBOX: dict[str, tuple[str, str, str, bool, str, str | None, str, list[str], float]] = {
    "furnace_noise": (
        "Furnace making a loud banging noise", "booking_request", "high", False,
        "Hi,\n\nOur furnace started making a loud bang every time it kicks on. Is this something you can look at "
        "this week? We're usually home after 3pm.\n\nThanks,\n{first}",
        "Hi {first},\n\nThanks for getting in touch, and sorry to hear about the noise. A bang when the furnace "
        "starts can have a few causes, so it's worth having a technician take a look soon.\n\nWe have afternoon "
        "openings this week. A standard call-out visit is $89 and includes the first 30 minutes of labor. Reply "
        "with a time after 3pm that suits you, or call us at (212) 555-0100, and we'll confirm the visit." + SIGN_OFF,
        "Customer reports a loud bang when the furnace starts and asks for a visit this week after 3pm.", [], 0.86,
    ),
    "boiler_price": (
        "Price for annual boiler service?", "pricing", "normal", False,
        "Hello, how much do you charge for the yearly boiler service? And do you have anything in early "
        "December?\n\n{first} {last}",
        "Hi {first},\n\nThanks for your message. Our annual boiler service costs $129. We have openings in early "
        "December; reply with a couple of days that work for you and we'll book the visit." + SIGN_OFF,
        "Customer asks for the annual boiler service price and early-December availability.", [], 0.93,
    ),
    "reschedule": (
        "Can I move my appointment?", "reschedule", "normal", False,
        "Hi there,\n\nSomething came up at work. Could we move my appointment to sometime next week instead? Any "
        "morning works.\n\nThank you,\n{first}",
        "Hi {first},\n\nNo problem at all. We'll move your visit to a morning next week. A member of our team will "
        "reply shortly with the new time to confirm." + SIGN_OFF,
        "Customer asks to move an existing appointment to a morning next week.", [], 0.78,
    ),
    "water_heater_quote": (
        "Quote for a new water heater", "booking_request", "normal", False,
        "Our water heater is 14 years old and starting to make noise. How much would a replacement cost? We have "
        "a 50 gallon gas tank.\n\n{first}",
        "Hi {first},\n\nThanks for reaching out. Quotes for installations are free, and the price depends on the "
        "model you choose and the work your space needs. The quickest way to an exact figure is a short in-home "
        "estimate. Reply with a day and time that suit you, and we'll book it." + SIGN_OFF,
        "Customer with a 14-year-old 50-gallon gas water heater asks for a replacement price.", [], 0.84,
    ),
    "saturday_hours": (
        "Are you open Saturday?", "question", "low", False,
        "Hi, are you open this Saturday? I'd like to drop by about a part.\n\n{first}",
        "Hi {first},\n\nYes, we're open on Saturday from 9am to 1pm. We're closed on Sundays." + SIGN_OFF,
        "Customer asks whether the office is open on Saturday.", [], 0.95,
    ),
    "complaint": (
        "Very unhappy with yesterday's visit", "complaint", "urgent", True,
        "The technician who came yesterday left muddy footprints all over our carpet and the furnace is still "
        "making the same noise. I expect someone to make this right.\n\n{first} {last}",
        "Hi {first},\n\nI'm very sorry about yesterday's visit. That is not the experience we want for you. A "
        "manager will contact you today to arrange a follow-up visit at no charge and to discuss the carpet." +
        SIGN_OFF,
        "Customer complains about a messy technician visit and an unresolved furnace noise; wants it made right.",
        ["needs_human"], 0.62,
    ),
    "double_charge": (
        "Charged twice on my card", "billing_dispute", "high", True,
        "Hi, I see two charges of $89 on my card for the same visit on the 12th. Can you please refund one of "
        "them?\n\n{first}",
        "Hi {first},\n\nThank you for letting us know, and sorry for the trouble. I've passed your message to our "
        "billing team, who will check the charges and get back to you." + SIGN_OFF,
        "Customer reports a duplicate $89 card charge and asks for a refund.", ["needs_human"], 0.58,
    ),
    "clogged_sink": (
        "Kitchen sink clogged", "booking_request", "high", False,
        "Our kitchen sink has been draining very slowly for a week and now it's completely backed up. Can someone "
        "come out tomorrow?\n\nThanks, {first}",
        "Hi {first},\n\nSorry to hear about the sink. We do drain cleaning every day. A standard call-out visit is "
        "$89 and includes the first 30 minutes of labor. Reply with a time that works tomorrow, or call (212) "
        "555-0100 for the fastest booking." + SIGN_OFF,
        "Customer's kitchen sink is fully backed up; asks for a visit tomorrow.", [], 0.88,
    ),
    "tankless": (
        "Do you install tankless water heaters?", "question", "normal", False,
        "Hi, do you install and service tankless water heaters? We're thinking about switching.\n\n{first}",
        "Hi {first},\n\nThanks for your question. Yes, we can help you compare a tankless water heater with a "
        "traditional tank and give you a free installation quote. Reply with a good time for a short call." +
        SIGN_OFF,
        "Customer asks whether Acme installs and services tankless water heaters.",
        ["ungrounded", "low_confidence"], 0.41,
    ),
    "thank_you": (
        "Thank you!", "other", "low", False,
        "Just wanted to say thanks to your plumber for fixing our leak so quickly yesterday. Great service!\n\n"
        "{first}",
        "Hi {first},\n\nThank you for the kind words! We'll pass them on to the team. We're glad everything is "
        "working again." + SIGN_OFF,
        "Customer thanks the plumber for a quick leak repair.", [], 0.97,
    ),
    "cancel": (
        "Please cancel my appointment", "cancellation", "normal", True,
        "Hi, please cancel my appointment for next week. We found someone closer to us.\n\n{first}",
        "Hi {first},\n\nThanks for letting us know. We'll cancel your upcoming appointment, and you'll get a "
        "confirmation email once it's done. If you need us in the future, we're here to help." + SIGN_OFF,
        "Customer asks to cancel next week's appointment.", [], 0.81,
    ),
    "unsubscribe": (
        "Unsubscribe", "unsubscribe", "low", False,
        "Please remove me from your email list.\n\n{first}",
        None,
        "Customer asks to be removed from the email list.", [], 0.0,
    ),
}  # fmt: skip
# The most recent threads, newest first: these are the ones waiting for human review.
INBOX_PENDING = ("tankless", "complaint", "furnace_noise", "boiler_price", "reschedule", "water_heater_quote",
                 "double_charge")  # fmt: skip
INBOX_OLDER = ("clogged_sink", "saturday_hours", "thank_you", "water_heater_quote", "boiler_price", "cancel",
               "furnace_noise", "unsubscribe", "clogged_sink", "reschedule", "saturday_hours", "thank_you",
               "furnace_noise", "boiler_price", "double_charge", "clogged_sink", "water_heater_quote",
               "saturday_hours", "thank_you", "reschedule", "boiler_price", "clogged_sink", "furnace_noise",
               "cancel", "water_heater_quote", "thank_you", "saturday_hours", "double_charge",
               "boiler_price")  # fmt: skip


# ================================================================================================ builder
class _Builder:
    def __init__(self, anchor: date) -> None:
        self.anchor = anchor
        self.rng = random.Random(SEED)  # noqa: S311 - deterministic demo data, not security
        self.contacts: list[Contact] = []
        self.calls: list[Call] = []
        self.bookings: list[Booking] = []
        self.call_campaigns: list[CallCampaign] = []
        self.email_campaigns: list[EmailCampaign] = []
        self.threads: list[Thread] = []
        self.tx_emails: list[TxEmail] = []
        self.busy: dict[str, list[tuple[datetime, datetime]]] = {k: [] for k in RESOURCES}
        self._event_n = 0
        self._member_n = 0
        self._message_n = 0

    def day(self, offset: int) -> date:
        return self.anchor + timedelta(days=offset)

    # ------------------------------------------------------------------------------------------ contacts
    def build_contacts(self) -> None:
        rng = self.rng
        pairs = [(f, la) for f in FIRST_NAMES for la in LAST_NAMES]
        rng.shuffle(pairs)
        used_emails: set[str] = set()
        for i in range(N_PAST + N_NEW):
            n = i + 1
            first, last = pairs[i]
            email = f"{first}.{last}@example.com".lower()
            k = 2
            while email in used_emails:
                email = f"{first}.{last}{k}@example.com".lower()
                k += 1
            used_emails.add(email)
            phone = f"+1{AREA_CODES[i // 40]}5550{101 + i % 40:03d}"
            new = i >= N_PAST
            if n == 1:
                created = anchor_marker(self.anchor)
            elif new:
                d = self.day(-rng.randint(1, WINDOW_DAYS - 1))
                created = local(d, rng.randint(8, 20), rng.randint(0, 59), rng.randint(0, 59))
            else:
                d = self.day(-rng.randint(45, 900))
                created = local(d, rng.randint(9, 17), rng.randint(0, 59), rng.randint(0, 59))
            if new:
                tags = ["new"]
                source = rng.choice(("website_form", "website_form", "google_ads", "referral"))
            else:
                hvac, plumbing = rng.random() < 0.65, rng.random() < 0.5
                if not (hvac or plumbing):
                    hvac = True
                tags = [t for t, on in (("hvac", hvac), ("plumbing", plumbing)) if on]
                if rng.random() < 0.3:
                    tags.append("club")
                if rng.random() < 0.25:
                    tags.append("water_heater")
                source = rng.choice(("legacy_import", "legacy_import", "phone", "website_form", "referral"))
            self.contacts.append(
                Contact(
                    n=n,
                    id=sid("contact", n),
                    first=first,
                    last=last,
                    email=email,
                    phone=phone,
                    created_at=created,
                    source=source,
                    tags=tags,
                    new_lead=new,
                    lead_id=sid("lead", n),
                    lead_status="new" if new else "won",
                    lead_score=rng.randint(35, 70) if new else rng.randint(55, 95),
                    lead_source=source,
                    lead_updated_at=created,
                )
            )

    # ------------------------------------------------------------------------------------------ bookings
    def _free(self, resource: str, start: datetime, end: datetime) -> bool:
        return all(end <= s or start >= e for s, e in self.busy[resource])

    def book(self, topic: str, contact: Contact, earliest: datetime, *, source_type: str, source_id: str | None,
             created_by: str, created_at: datetime, urgent: bool = False) -> Booking:  # fmt: skip
        service, resource, minutes = SERVICES[topic]
        _, _, _, open_h, close_h = RESOURCES[resource]
        rng = self.rng
        d = local_date(earliest)
        if not urgent:
            d += timedelta(days=rng.randint(1, 5))
        while True:
            if is_workday(d):
                hours = list(range(open_h, close_h))
                if not urgent:
                    rng.shuffle(hours)
                for h in hours:
                    for minute in (0, 30):
                        start = local(d, h, minute)
                        end = start + timedelta(minutes=minutes)
                        if start <= earliest + timedelta(hours=2) or end > local(d, close_h):
                            continue
                        if self._free(resource, start, end):
                            self.busy[resource].append((start, end))
                            n = len(self.bookings) + 1
                            b = Booking(n, sid("booking", n), resource, contact, service, start, end, "confirmed",
                                        source_type, source_id, created_by, created_at)  # fmt: skip
                            self.bookings.append(b)
                            return b
            d += timedelta(days=1)

    def settle_bookings(self) -> None:
        """Past visits are completed (a few no-shows); upcoming ones confirmed; a few were canceled."""
        rng = self.rng
        for b in sorted(self.bookings, key=lambda x: x.n):
            b.confirmed_at = b.created_at + timedelta(seconds=2)
            roll = rng.random()
            if local_date(b.start) < self.anchor:
                if roll < 0.06:
                    b.status, b.cancel_reason = "canceled", rng.choice(("requested", "no_longer_needed"))
                    b.canceled_at = min(b.start - timedelta(hours=rng.randint(4, 30)), self._now())
                elif roll < 0.11:
                    b.status = "no_show"
                else:
                    b.status = "completed"
                b.updated_at = b.canceled_at or b.end + timedelta(minutes=rng.randint(5, 50))
            else:
                if roll < 0.08 and b.created_at < local(self.anchor, 0):
                    b.status, b.cancel_reason = "canceled", rng.choice(("rescheduled_elsewhere", "requested"))
                    b.canceled_at = b.created_at + timedelta(hours=rng.randint(3, 20))
                    b.canceled_at = min(b.canceled_at, local(self.anchor, 0) - timedelta(minutes=17))
                b.updated_at = b.canceled_at or b.confirmed_at
            c = b.contact
            if b.status == "completed":
                c.lead_status, c.lead_booking_id = "won", b.id
            elif b.status in ("confirmed", "no_show"):
                c.lead_status, c.lead_booking_id = "booked", b.id
            c.lead_updated_at = max(c.lead_updated_at or b.created_at, b.updated_at or b.created_at)

    def _now(self) -> datetime:
        """Latest moment any seeded activity may happen: 00:45 UTC on the anchor date (evening before, in NY)."""
        return datetime.combine(self.anchor, time(0, 45), UTC)

    # ------------------------------------------------------------------------------------------ calls
    def _new_call(self, direction: str, contact: Contact, topic: str, created_at: datetime) -> Call:
        n = len(self.calls) + 1
        call = Call(n=n, id=sid("call", n), direction=direction, contact=contact, topic=topic, created_at=created_at)
        self.calls.append(call)
        return call

    def _talk(self, call: Call, lines: list[tuple[str, str]], answered: datetime, human_seconds: int = 0) -> None:
        rng = self.rng
        segments, total_ms, barge = _seg_list(lines, rng)
        call.segments, call.barge_ins = segments, barge
        ai_seconds = math.ceil(total_ms / 1000) + rng.randint(1, 3)
        call.voice_ended_at = answered + timedelta(seconds=ai_seconds, milliseconds=400)
        call.duration = ai_seconds + human_seconds
        call.ended_at = answered + timedelta(seconds=call.duration + 1)
        call.voice_end_reason = "completed"

    def _cost(self, call: Call) -> int:
        if call.answered_at is None:
            return 1_500
        return 2_000 + call.duration * 650

    def _apply_outcome(self, call: Call, outcome: str, campaign: CallCampaign | None) -> None:
        call.outcome = outcome
        call.qualified = {
            "appointment_requested": True,
            "interested": True,
            "callback_requested": True,
            "not_interested": False,
            "wrong_number": False,
            "opted_out": False,
        }.get(outcome)
        c = call.contact
        c.last_outcome, c.lead_call_id = outcome, call.id
        if campaign is not None:
            c.lead_campaign_id = campaign.id
        if outcome in ("appointment_requested", "interested", "callback_requested"):
            c.lead_status = "qualified" if c.lead_status in ("new", "contacted") else c.lead_status
            c.lead_qualified = True
        elif outcome in ("not_interested", "wrong_number", "opted_out") and c.lead_status in ("new", "contacted"):
            c.lead_status, c.lead_qualified = "unqualified", False
        elif c.lead_status == "new":
            c.lead_status = "contacted"
        c.lead_updated_at = max(c.lead_updated_at or call.ended_at, call.ended_at)  # type: ignore[type-var]

    def outbound_call(self, campaign: CallCampaign, member: CampaignMember, at: datetime) -> Call:
        rng = self.rng
        c = member.contact
        call = self._new_call("outbound", c, campaign.topic, at)
        call.campaign, call.member, call.attempt = campaign, member, member.attempts
        call.initiated_at = at + timedelta(milliseconds=rng.randint(300, 900))
        call.started_at = call.initiated_at + timedelta(seconds=rng.randint(1, 2))
        roll = rng.random()
        if roll < 0.24:  # nobody picks up
            call.state, call.end_reason, call.provider_status = "failed", "no_answer", "no-answer"
            call.ended_at = call.started_at + timedelta(seconds=rng.randint(25, 32))
        elif roll < 0.29:
            call.state, call.end_reason, call.provider_status = "failed", "busy", "busy"
            call.ended_at = call.started_at + timedelta(seconds=rng.randint(3, 6))
        else:
            answered = call.started_at + timedelta(seconds=rng.randint(5, 22))
            call.answered_at = answered
            if roll < 0.46:  # answering machine: the agent leaves a short message
                _, subject = OUTBOUND_PITCH[campaign.topic]
                call.end_reason = "answering_machine"
                call.outcome = "voicemail"
                self._talk(call, [("agent", f"Hi {c.first}, this is the automated assistant from Acme Home "
                                            f"Services, calling about {subject}. Give us a call back at "
                                            "212-555-0100 whenever it suits you. Thanks, and have a great day!")],
                           answered + timedelta(seconds=3))  # fmt: skip
                call.ended_at = (call.voice_ended_at or answered) + timedelta(seconds=1)
                call.duration = int((call.ended_at - answered).total_seconds())
            else:
                outcome = rng.choices(
                    ("appointment_requested", "callback_requested", "interested", "not_interested", "escalated",
                     "wrong_number", "opted_out", "other"),
                    (0.31, 0.16, 0.10, 0.27, 0.05, 0.04, 0.03, 0.04),
                )[0]  # fmt: skip
                booking = None
                if outcome == "appointment_requested":
                    booking = self.book(campaign.topic, c, answered, source_type="ai_tool", source_id=str(call.id),
                                        created_by="service:tool-execution",
                                        created_at=answered + timedelta(seconds=rng.randint(50, 80)))  # fmt: skip
                    call.booking = booking
                alt = slot_phrase(local(_next_workday(local_date(answered)), rng.choice((9, 11, 14))), answered)
                lines = _outbound_lines(campaign.topic, outcome, c, answered, booking, alt, rng)
                human = rng.randint(140, 420) if outcome == "escalated" else 0
                self._talk(call, lines, answered, human)
                if outcome == "escalated":
                    call.handoff_state, call.voice_end_reason = "transferring", "handoff"
                    call.handoff_at = (call.voice_ended_at or answered) - timedelta(seconds=1)
                elif outcome == "opted_out":
                    call.state, call.opted_out, call.voice_end_reason = "opted_out", True, "opted_out"
                    call.ended_at = call.voice_ended_at
                    c.phone_opt_out = (call.ended_at, call.id)  # type: ignore[assignment]
                self._apply_outcome(call, outcome, campaign)
            if call.outcome == "voicemail":
                self._apply_outcome(call, "voicemail", campaign)
            if call.state not in ("failed", "opted_out"):
                call.state = "completed"
        call.cost_micros = self._cost(call)
        return call

    def build_inbound(self) -> None:
        rng = self.rng
        past = [c for c in self.contacts if not c.new_lead and c.n != 1]
        new_callers = [c for c in self.contacts if c.new_lead][:14]
        topics = list(INBOUND_TOPICS)
        weights = [INBOUND_TOPICS[t][1] for t in topics]
        plan: list[tuple[datetime, Contact, str]] = []
        for c in new_callers:  # first contact with these leads was a phone call
            topic = rng.choices(topics[:8], weights[:8])[0]
            plan.append((c.created_at - timedelta(minutes=rng.randint(2, 4)), c, topic))
        k = 0
        for off in range(WINDOW_DAYS):  # every day has calls: the AI answers the main line around the clock
            d = self.day(-off)
            recent = 1 - off / WINDOW_DAYS  # gentle growth towards today
            if off == 0:  # after-hours calls the evening before, which are already "today" in UTC
                times = [datetime.combine(d, time(0, rng.randint(5, 40), rng.randint(0, 59)), UTC)]
            elif is_workday(d):
                n = 1 + (rng.random() < 0.2 + 0.35 * recent) + (rng.random() < 0.1 + 0.15 * recent)
                times = [local(d, rng.randint(7, 20), rng.randint(0, 59), rng.randint(0, 59)) for _ in range(n)]
            else:
                times = [local(d, rng.randint(9, 18), rng.randint(0, 59), rng.randint(0, 59))]
            for at in times:
                topic = rng.choices(topics, weights)[0] if k else "no_heat"
                plan.append((at, past[(k * 7 + 3) % len(past)], topic))
                k += 1
        for at, c, topic in sorted(plan, key=lambda p: p[0]):
            outcome, _ = INBOUND_TOPICS[topic]
            call = self._new_call("inbound", c, topic, at)
            call.started_at = at + timedelta(milliseconds=rng.randint(200, 700))
            answered = call.started_at + timedelta(seconds=1)
            call.answered_at = answered
            booking = None
            if outcome == "appointment_requested":
                booking = self.book(topic, c, answered, source_type="ai_tool", source_id=str(call.id),
                                    created_by="service:tool-execution",
                                    created_at=answered + timedelta(seconds=rng.randint(45, 75)),
                                    urgent=topic in ("no_heat", "water_heater_leak"))  # fmt: skip
                call.booking = booking
            human = rng.randint(150, 480) if outcome == "escalated" else 0
            self._talk(call, _inbound_lines(topic, c, answered, booking, rng), answered, human)
            if outcome == "escalated":
                call.handoff_state, call.voice_end_reason = "transferring", "handoff"
                call.handoff_at = (call.voice_ended_at or answered) - timedelta(seconds=1)
            call.state, call.end_reason, call.provider_status = "completed", "completed", "completed"
            self._apply_outcome(call, outcome, None)
            call.cost_micros = self._cost(call)

    # ------------------------------------------------------------------------------------------ campaigns
    def _event(self, camp: CallCampaign, kind: str, actor: str, at: datetime, **kw: Any) -> None:
        self._event_n += 1
        camp.events.append(CampaignEvent(sid("campaign_event", self._event_n), kind, actor, at, **kw))

    def _audience(self, tag: str | None, limit: int, before: datetime, *, exclude: set[uuid.UUID]) -> list[Contact]:
        pool = [
            c
            for c in self.contacts
            if (tag is None or tag in c.tags)
            and c.created_at < before
            and c.id not in exclude
            and (c.phone_opt_out is None or c.phone_opt_out[0] > before)
            and c.n != 1
        ]
        self.rng.shuffle(pool)
        return sorted(pool[:limit], key=lambda c: c.n)

    def call_campaign(  # noqa: PLR0912, PLR0915 - one linear simulation of a campaign's dialing
            self, *, n: int, key: str, name: str, description: str, topic: str, segment: str,
                      state: str, launch_offset: int, audience: int, process: int | None = None,
                      max_attempts: int = 2, window: tuple[str, str] = ("10:00", "19:00"),
                      pause_offset: int | None = None,
                      exclude: set[uuid.UUID] | None = None) -> CallCampaign:  # fmt: skip
        rng = self.rng
        mgr = f"user:{CAMPAIGN_MANAGER}"
        launch_day = _workday_on_or_before(self.day(launch_offset))
        launched = local(launch_day, int(window[0][:2]), rng.randint(2, 20))
        camp = CallCampaign(
            n=n, id=sid("call_campaign", n), key=key, name=name, description=description, topic=topic, state=state,
            segment_id=segment_id(segment), created_at=launched - timedelta(days=rng.randint(2, 5), hours=3),
            window_start=window[0], window_end=window[1], max_attempts=max_attempts, launched_at=launched,
            snapshot_status="completed",
        )  # fmt: skip
        self.call_campaigns.append(camp)
        self._event(camp, "state.schedule", mgr, launched - timedelta(minutes=rng.randint(20, 90)),
                    from_state="draft", to_state="scheduled")  # fmt: skip
        self._event(camp, "state.launch", mgr, launched, from_state="scheduled", to_state="running")
        members = self._audience(SEGMENTS[segment][3], audience, launched, exclude=exclude or set())
        camp.snapshot_total = len(members)
        snap_at = launched + timedelta(seconds=rng.randint(8, 25))
        self._event(camp, "snapshot_completed", "service:campaign-service", snap_at, data={"total": len(members)})
        for c in members:
            self._member_n += 1
            camp.members.append(CampaignMember(self._member_n, sid("campaign_contact", self._member_n), c, snap_at))
        stop = self.day(-1) if pause_offset is None else self.day(pause_offset)
        stop_at = local(stop, 15, 10) if pause_offset is not None else local(stop, int(window[1][:2]))
        if state == "completed":
            stop, stop_at = self.day(-1), local(self.day(-1), 23)
        days = _workdays(launch_day, stop)
        todo = camp.members[: process if process is not None else len(camp.members)]
        spread = min(len(days), 6) if state == "completed" else max(1, len(days))
        per_day = math.ceil(len(todo) / spread)
        queue: list[tuple[datetime, CampaignMember]] = []
        for k, m in enumerate(todo):
            d = days[min(k // per_day, len(days) - 1)]
            start_h = int(window[0][:2]) + (1 if d == launch_day else 0)
            queue.append((local(d, rng.randint(start_h, int(window[1][:2]) - 1), rng.randint(0, 59),
                                rng.randint(0, 59)), m))  # fmt: skip
        while queue:
            queue.sort(key=lambda q: (q[0], q[1].n))
            at, m = queue.pop(0)
            if at > stop_at:
                m.state, m.next_attempt_at = "retry_wait", at
                continue
            m.attempts += 1
            m.dispatched_at = at
            self._event(camp, "call_dispatched", "service:campaign-dispatcher", at, contact_id=m.contact.id,
                        data={"attempt": m.attempts})  # fmt: skip
            call = self.outbound_call(camp, m, at + timedelta(seconds=1))
            m.last_call_id = call.id
            final = call.final_state
            if final == "completed":
                m.state, m.outcome = "completed", "completed"
            elif final == "opted_out":
                m.state, m.outcome, m.skip_reason = "skipped", "opted_out", "opted_out"
            elif m.attempts < max_attempts:
                m.state, m.outcome = "retry_wait", final
                due = (call.ended_at or at) + timedelta(seconds=camp.retry_delay_seconds)
                nd = local_date(due)
                if not is_workday(nd):
                    nd = _next_workday(nd)
                m.next_attempt_at = local(nd, rng.randint(int(window[0][:2]), int(window[1][:2]) - 1),
                                          rng.randint(0, 59), rng.randint(0, 59))  # fmt: skip
                queue.append((m.next_attempt_at, m))
            else:
                m.state, m.outcome = "failed", final
            if m.state in ("completed", "skipped", "failed"):
                m.completed_at, m.next_attempt_at = call.ended_at, None
            m.updated_at = call.ended_at
            self._event(camp, "call_completed", "service:campaign-service", call.ended_at or at,
                        contact_id=m.contact.id,
                        call_id=call.id, data={"final_state": final, "contact_state": m.state,
                                               "attempt": m.attempts})  # type: ignore[arg-type]  # fmt: skip
        for m in camp.members:
            m.updated_at = m.updated_at or snap_at
        last = max((e.created_at for e in camp.events), default=launched)
        if state == "completed":
            camp.completed_at = last + timedelta(seconds=rng.randint(20, 50))
            self._event(camp, "state.complete", "service:campaign-dispatcher", camp.completed_at,
                        from_state="running", to_state="completed", data={"reason": "audience_exhausted"})  # fmt: skip
            camp.updated_at, camp.version = camp.completed_at, 4
        elif state == "paused":
            camp.paused_at = max(stop_at, last + timedelta(minutes=3))
            self._event(camp, "state.pause", mgr, camp.paused_at, from_state="running", to_state="paused",
                        data={"reason": "crew_fully_booked"})  # fmt: skip
            camp.updated_at, camp.version = camp.paused_at, 4
        else:
            camp.updated_at, camp.version = last, 3
        return camp

    def build_call_campaigns(self) -> None:
        rng = self.rng
        self.call_campaign(
            n=7, key="ac_check", name="End-of-season AC check",
            description="Calls past cooling customers to clean and check their AC before winter.",
            topic="ac_tuneup", segment="hvac", state="completed", launch_offset=-41, audience=20,
        )  # fmt: skip
        c1 = self.call_campaign(
            n=1, key="fall_tuneup", name="Fall furnace tune-up reminders",
            description="Calls past heating customers to book a tune-up before the first cold snap.",
            topic="furnace_tuneup", segment="hvac", state="completed", launch_offset=-27, audience=24,
        )  # fmt: skip
        c2 = self.call_campaign(
            n=2, key="club_renewal", name="Comfort Club renewal calls",
            description="Renews annual maintenance plans and books the member's winter visit.",
            topic="club_renewal", segment="club", state="completed", launch_offset=-18, audience=18, max_attempts=3,
            window=("09:00", "18:00"),
        )  # fmt: skip
        c3 = self.call_campaign(
            n=3, key="water_heater", name="Water heater replacement follow-up",
            description="Offers free replacement estimates to customers with water heaters over ten years old.",
            topic="water_heater", segment="water_heater", state="paused", launch_offset=-9, audience=22,
            process=11, window=("09:00", "18:00"), pause_offset=-4,
        )  # fmt: skip
        c4 = self.call_campaign(
            n=4, key="furnace_safety", name="Winter furnace safety check",
            description="Offers a furnace safety check with a carbon monoxide test to heating customers.",
            topic="furnace_safety", segment="hvac", state="running", launch_offset=-3, audience=36, process=15,
            exclude={m.contact.id for m in c1.members},
        )  # fmt: skip
        del c2, c3, c4
        # Not launched yet: a scheduled and a draft campaign (no audience snapshot, no calls).
        nxt = self.anchor + timedelta(days=7 - self.anchor.weekday())  # next Monday
        sched_created = local(self.day(-2), 11, 24)
        self.call_campaigns.append(CallCampaign(
            n=5, id=sid("call_campaign", 5), key="drain_special", name="Holiday drain cleaning special",
            description="Before-the-holidays drain cleaning offer for past plumbing customers.", topic="drain",
            state="scheduled", segment_id=segment_id("plumbing"), created_at=sched_created,
            start_at=local(nxt, 10), end_at=local(nxt + timedelta(days=11), 19), updated_at=sched_created +
            timedelta(minutes=12), version=2,
            events=[CampaignEvent(sid("campaign_event", 9001), "state.schedule", f"user:{CAMPAIGN_MANAGER}",
                                  sched_created + timedelta(minutes=12), "draft", "scheduled")],
        ))  # fmt: skip
        draft_created = local(self.day(-1), 16, 5)
        self.call_campaigns.append(CallCampaign(
            n=6, id=sid("call_campaign", 6), key="spring_ac", name="Spring AC pre-season check",
            description="Draft: book AC checks for March and April before the first heat wave.", topic="estimate",
            state="draft", segment_id=segment_id("hvac"), created_at=draft_created, updated_at=draft_created,
            window_start="09:00", window_end="18:00", max_attempts=rng.choice((2, 3)),
        ))  # fmt: skip

    # ------------------------------------------------------------------------------------------ fillers
    def fill_bookings(self) -> None:
        """Bookings made by staff or the website widget, so the current and next week look busy."""
        rng = self.rng
        week0 = self.anchor - timedelta(days=self.anchor.weekday())
        lo, hi = local(week0, 0), local(week0 + timedelta(days=14), 0)
        pool = [c for c in self.contacts if c.n != 1 and c.lead_booking_id is None]
        rng.shuffle(pool)
        topics = ("furnace_tuneup", "drain", "boiler_service", "estimate", "water_heater_leak", "furnace_safety")
        k = 0
        while sum(1 for b in self.bookings if lo <= b.start < hi) < 24:
            c = pool[k]
            k += 1
            created = local(_workday_on_or_before(self.day(-rng.randint(1, 6))), rng.randint(9, 17),
                            rng.randint(0, 59))  # fmt: skip
            created = max(created, c.created_at + timedelta(minutes=10))
            earliest = max(created, local(week0 + timedelta(days=rng.randint(0, 9)), 7))
            if rng.random() < 0.55:
                src, by = "user", f"user:{CALL_AGENT_USER}"
            else:
                src, by = "api", "service:website-widget"
            b = self.book(rng.choice(topics), c, earliest - timedelta(days=1), source_type=src, source_id=None,
                          created_by=by, created_at=created)  # fmt: skip
            if b.start >= hi:  # keep fillers inside the two weeks
                self.bookings.pop()
                self.busy[b.resource].pop()
                k -= 0
            elif c.new_lead:
                c.lead_status = "qualified"

    # ------------------------------------------------------------------------------------------ email
    def email_campaign(self, *, n: int, key: str, name: str, template: str, segment: str | None, state: str,
                       launch_offset: int, limit: int) -> EmailCampaign:  # fmt: skip
        rng = self.rng
        launched = local(_workday_on_or_before(self.day(launch_offset)), 10, rng.randint(0, 15))
        tag = SEGMENTS[segment][3] if segment else None
        pool = [
            c
            for c in self.contacts
            if (tag is None or tag in c.tags)
            and c.created_at < launched
            and c.email_suppressed is None
            and c.n != 1
        ]
        rng.shuffle(pool)
        members = sorted(pool[:limit], key=lambda c: c.n)
        camp = EmailCampaign(n, sid("email_campaign", n), key, name, template,
                             segment_id(segment) if segment else ALL_CONTACTS_SEGMENT, state,
                             launched - timedelta(days=rng.randint(1, 4), hours=2), launched, None)  # fmt: skip
        t = launched + timedelta(seconds=30)
        for c in members:
            self._message_n += 1
            queued = t
            accepted = queued + timedelta(milliseconds=rng.randint(400, 1500))
            t += timedelta(seconds=2)
            roll = rng.random()
            delivered: datetime | None = accepted + timedelta(seconds=rng.randint(2, 9))
            state_at: datetime = delivered  # type: ignore[assignment]
            msg_state, supp = "delivered", None
            if roll < 0.022:
                msg_state, supp, delivered = "bounced", "bounce", None
                state_at = accepted + timedelta(seconds=rng.randint(3, 40))
            elif roll < 0.026:
                msg_state, supp = "complained", "complaint"
                state_at = accepted + timedelta(hours=rng.randint(2, 30))
            elif roll < 0.04:
                msg_state, supp = "unsubscribed", "opt_out"
                state_at = accepted + timedelta(hours=rng.randint(1, 48))
            state_at = min(state_at, self._now())
            mid = sid("email_message", self._message_n)
            camp.messages.append(EmailMessage(self._message_n, mid, c, msg_state, queued, accepted, delivered,
                                              state_at, supp))  # fmt: skip
            if supp:
                c.email_suppressed = (supp, state_at, mid)
        if state == "completed":
            camp.completed_at = t + timedelta(seconds=rng.randint(10, 40))
        self.email_campaigns.append(camp)
        return camp

    def build_email_campaigns(self) -> None:
        self.email_campaign(n=4, key="ac_check", name="End-of-summer AC check", template="ac_check",
                            segment="hvac", state="completed", launch_offset=-38, limit=200)  # fmt: skip
        self.email_campaign(n=1, key="tuneup", name="Fall furnace tune-up", template="tuneup", segment="hvac",
                            state="completed", launch_offset=-24, limit=200)  # fmt: skip
        self.email_campaign(n=2, key="pipes", name="Winter pipe-freeze prevention tips", template="pipes",
                            segment=None, state="completed", launch_offset=-12, limit=400)  # fmt: skip
        self.email_campaign(n=3, key="water_heater", name="Holiday water heater savings", template="water_heater",
                            segment="plumbing", state="running", launch_offset=-1, limit=200)  # fmt: skip

    # ------------------------------------------------------------------------------------------ inbox
    def build_threads(self) -> None:  # noqa: PLR0915 - one linear story per thread
        rng = self.rng
        reviewer = str(EMAIL_REVIEWER)
        pool = [c for c in self.contacts if c.n != 1]
        rng.shuffle(pool)
        plan: list[tuple[str, datetime]] = []
        for i, key in enumerate(INBOX_PENDING):  # newest first, within the last ~2 days
            at = self._now() - timedelta(hours=1 + i * 5 + rng.randint(0, 3), minutes=rng.randint(0, 59))
            plan.append((key, at))
        for i, key in enumerate(INBOX_OLDER):
            d = _workday_on_or_before(self.day(-3 - i * (WINDOW_DAYS - 5) // len(INBOX_OLDER)))
            plan.append((key, local(d, rng.randint(7, 20), rng.randint(0, 59), rng.randint(0, 59))))
        plan.sort(key=lambda p: p[1])
        for n, (key, received) in enumerate(plan, start=1):
            subject, intent, priority, sensitive, inbound, draft, summary, flags, conf = INBOX[key]
            c = pool[n % len(pool)]
            tid = sid("inbox_thread", n)
            rfc = f"<showcase-{n:03d}.{c.n}@example.com>"
            msg = InboxMessage(sid("inbox_message", n), "inbound", c.email, c.name, MAILBOX_ADDRESS, subject,
                               inbound.format(first=c.first, last=c.last), received, rfc)  # fmt: skip
            th = Thread(n, tid, c, key, subject, intent, priority, "open", sensitive, [msg], draft_summary=summary)
            self.threads.append(th)
            pending = key in INBOX_PENDING and received > self._now() - timedelta(days=2)
            if draft is None:  # unsubscribe requests are handled without a reply
                th.status = "closed"
                continue
            gen = received + timedelta(seconds=rng.randint(35, 95))
            th.draft_id, th.draft_created_at = sid("inbox_draft", n), received + timedelta(seconds=2)
            body = draft.format(first=c.first, last=c.last)
            cites = [] if "ungrounded" in flags else [
                {"chunk_id": str(sid("knowledge_chunk_ref", 1 + n % 2)), "source_id": str(KNOWLEDGE_SOURCE_ID),
                 "source_name": "Business FAQ"}
            ]  # fmt: skip
            th.draft_versions.append(DraftVersion(1, body, "ai", "ai-agent-service", gen,
                                                  {"confidence": conf, "flags": flags}, cites))  # fmt: skip
            act = self._draft_action
            act(th, "generated", 1, "service", "ai-agent-service", gen)
            th.draft_updated_at = gen
            if pending:
                th.status, th.draft_status = "awaiting_review", "pending_review"
                if key == "complaint":
                    th.escalated, th.escalation_reason = True, "complaint"
                    act(th, "escalated", 1, "user", reviewer, gen + timedelta(minutes=6), reason="complaint")
                    th.draft_updated_at = gen + timedelta(minutes=6)
                if key in ("furnace_noise", "double_charge"):
                    th.assigned_to = reviewer
                    act(th, "assigned", 1, "user", reviewer, gen + timedelta(minutes=3),
                        details={"assignee": reviewer})  # fmt: skip
                    th.draft_updated_at = gen + timedelta(minutes=3)
                if key == "boiler_price":  # a reviewer already tightened the AI wording
                    edited = gen + timedelta(minutes=9)
                    th.draft_versions.append(DraftVersion(
                        2, body.replace(" reply with a couple of days", " we can also do Saturday mornings. Reply "
                                        "with a couple of days"), "user", reviewer, edited, {}, []))  # fmt: skip
                    act(th, "edited", 2, "user", reviewer, edited)
                    th.draft_updated_at = edited
                continue
            if (key == "thank_you" and n % 2 == 0) or (key == "cancel" and n % 3 == 0):
                rejected = gen + timedelta(minutes=rng.randint(20, 200))
                th.status, th.draft_status, th.draft_updated_at = "closed", "rejected", rejected
                act(th, "rejected", 1, "user", reviewer, rejected, reason="handled_elsewhere")
                continue
            version = 1
            approved = gen + timedelta(minutes=rng.randint(12, 260))
            if rng.random() < 0.3:
                version = 2
                edited = approved - timedelta(minutes=rng.randint(1, 8))
                warmer = body.replace("Best regards,", "Thanks again,\n\nBest regards,")
                th.draft_versions.append(DraftVersion(2, warmer, "user", reviewer, edited, {}, []))
                act(th, "edited", 2, "user", reviewer, edited)
            sent = approved + timedelta(seconds=rng.randint(4, 15))
            act(th, "approved", version, "user", reviewer, approved)
            act(th, "sent", version, "system", "email-sender", sent, details={"state": "queued"})
            th.approved_version, th.approved_by, th.approved_at, th.sent_at = version, reviewer, approved, sent
            th.status, th.draft_status, th.draft_updated_at = "replied", "sent", sent
            reply = next(v.body for v in th.draft_versions if v.version == version)
            self._tx("reply", c, MAILBOX_ADDRESS, f"Re: {subject}", reply, sent + timedelta(seconds=2), "email_draft",
                     str(th.draft_id))  # fmt: skip
            th.reply_email_id = self.tx_emails[-1].id
            th.messages.append(InboxMessage(sid("inbox_message", 1000 + n), "outbound", MAILBOX_ADDRESS,
                                            "Acme Home Services support", c.email, f"Re: {subject}", reply, sent, None,
                                            in_reply_to=rfc, draft_version=version))  # fmt: skip

    def _tx(self, category: str, c: Contact, sender: str, subject: str, body: str, at: datetime, source_type: str,
            source_id: str) -> None:  # fmt: skip
        n = len(self.tx_emails) + 1
        delivered = at + timedelta(seconds=self.rng.randint(2, 9))
        self.tx_emails.append(TxEmail(n, sid("tx_email", n), category, c, sender, subject, body, at, delivered,
                                      source_type, source_id))  # fmt: skip

    def build_confirmations(self) -> None:
        """Every booking sends a confirmation email (and a cancellation notice when canceled)."""
        for b in sorted(self.bookings, key=lambda x: x.created_at):
            when = b.start.astimezone(TZ).strftime("%A, %B %-d at %-I:%M %p").replace(":00 ", " ")
            body = (f"Hi {b.contact.first},\n\nYour {b.service.lower()} is confirmed for {when}. A technician will "
                    "call about 30 minutes before arriving. Need to change the time? Reply to this email or call "
                    "(212) 555-0100.\n\nThe Acme Home Services team")  # fmt: skip
            self._tx("transactional", b.contact, EMAIL_REPLY_TO, "Your appointment is confirmed", body,
                     (b.confirmed_at or b.created_at) + timedelta(seconds=20), "booking", str(b.id))  # fmt: skip
            if b.status == "canceled" and b.canceled_at:
                self._tx("transactional", b.contact, EMAIL_REPLY_TO, "Your appointment was canceled",
                         f"Hi {b.contact.first},\n\nYour {b.service.lower()} on {when} has been canceled. We hope "
                         "to see you another time.\n\nThe Acme Home Services team", b.canceled_at +
                         timedelta(seconds=15), "booking", str(b.id))  # fmt: skip

    def _draft_action(self, th: Thread, action: str, version: int | None, actor_type: str, actor_id: str,
                      at: datetime, *, reason: str | None = None,
                      details: dict[str, Any] | None = None) -> None:  # fmt: skip
        n = th.n * 20 + len(th.draft_actions) + 1
        th.draft_actions.append(DraftAction(sid("draft_action", n), action, version, actor_type, actor_id, at, reason,
                                            details or {}))  # fmt: skip

    # ------------------------------------------------------------------------------------------ analytics
    def metrics(self) -> dict[tuple[date, str, str], int]:  # noqa: PLR0912 - one branch per counter
        out: dict[tuple[date, str, str], int] = {}
        first = self.anchor - timedelta(days=WINDOW_DAYS - 1)

        def bump(ts: datetime | None, metric: str, value: int = 1, dims: tuple[str, ...] = ("",)) -> None:
            if ts is None:
                return
            d = ts.astimezone(UTC).date()
            if first <= d <= self.anchor:
                for dim in dims:
                    out[(d, metric, dim)] = out.get((d, metric, dim), 0) + value

        for call in self.calls:
            dims = ("", f"campaign:{call.campaign.id}") if call.campaign else ("",)
            bump(call.created_at, "calls.attempted", 1, dims)
            if call.answered_at:
                bump(call.answered_at, "calls.answered", 1, dims)
            bump(call.ended_at, "calls.ended", 1, dims)
            if call.final_state in ("completed", "failed", "canceled"):
                bump(call.ended_at, f"calls.{call.final_state}", 1, dims)
            bump(call.ended_at, "calls.duration_seconds", call.duration, dims)
            bump(call.ended_at, "calls.cost_micros", call.cost_micros, dims)
            if call.opted_out:
                bump(call.ended_at, "calls.opted_out", 1, dims)
            if call.qualified:
                bump(call.ended_at, "calls.qualified", 1, dims)
        for b in self.bookings:
            bump(b.created_at, "bookings.created")
            bump(b.confirmed_at, "bookings.confirmed")
            if b.status == "canceled":
                bump(b.canceled_at, "bookings.canceled")
        for camp in self.email_campaigns:
            edims = ("", "category:campaign", f"campaign:{camp.id}")
            for m in camp.messages:
                bump(m.accepted_at, "emails.accepted", 1, edims)
                if m.delivered_at:
                    bump(m.delivered_at, "emails.delivered", 1, edims)
                if m.state in ("bounced", "complained", "unsubscribed"):
                    bump(m.state_changed_at, f"emails.{m.state}", 1, edims)
        for tx in self.tx_emails:
            dims = ("", f"category:{tx.category}")
            bump(tx.accepted_at, "emails.accepted", 1, dims)
            bump(tx.delivered_at, "emails.delivered", 1, dims)
        for th in self.threads:
            bump(th.messages[0].received_at, "drafts.requested") if th.draft_id else None
            for a in th.draft_actions:
                if a.action in ("generated", "approved", "rejected", "sent"):
                    bump(a.created_at, f"drafts.{a.action}")
                if a.action == "approved" and (th.approved_version or 1) > 1:
                    bump(a.created_at, "drafts.edited")
        for c in self.contacts:
            if c.new_lead:
                bump(c.created_at, "leads.created")
        for cc in self.call_campaigns:
            if cc.launched_at:
                bump(cc.launched_at, "campaigns.launched", 1, ("", "channel:call"))
        for ec in self.email_campaigns:
            bump(ec.launched_at, "campaigns.launched", 1, ("", "channel:email"))
        return out


@lru_cache(maxsize=4)
def build(anchor: date) -> Scenario:
    """The whole showcase for `anchor` (the UTC date of the first seeding run). Pure and deterministic."""
    b = _Builder(anchor)
    b.build_contacts()
    b.build_inbound()
    b.build_call_campaigns()
    b.fill_bookings()
    b.settle_bookings()
    b.build_email_campaigns()
    b.build_confirmations()
    b.build_threads()
    calls = sorted(b.calls, key=lambda c: c.n)
    sc = Scenario(anchor, b.contacts, b.call_campaigns, calls, b.bookings, b.email_campaigns, b.threads, b.metrics(),
                  b.tx_emails)
    first = anchor - timedelta(days=WINDOW_DAYS - 1)
    for a in acme_ai_calls(sc):  # AI usage comes from the same requests the AI Gateway seed writes
        d = a.at.astimezone(UTC).date()
        if not first <= d <= anchor:
            continue
        for dim in ("", f"purpose:{a.purpose}"):
            for metric, value in (("ai.requests", 1), ("ai.input_tokens", a.input_tokens),
                                  ("ai.output_tokens", a.output_tokens), ("ai.cost_micros", a.cost_micros),
                                  ("ai.latency_ms", a.latency_ms)):  # fmt: skip
                key = (d, metric, dim)
                sc.daily_metrics[key] = sc.daily_metrics.get(key, 0) + value
    return sc


# ============================================================================================== platform tenants
@dataclass(frozen=True, slots=True)
class PlatformTenant:
    """A fictional agency client for the platform console (tenant-service rows plus usage read models)."""

    n: int
    id: uuid.UUID
    slug: str
    name: str
    timezone: str
    industry: str
    status: str
    created_days_ago: int
    domain: str  # reserved `.example` TLD
    members: tuple[tuple[str, str, str], ...]  # (first, last, role key)
    calls_per_day: int  # weekday average; 0 = no calling
    emails_per_week: int
    drafts_per_day: float

    @property
    def voice_agent_id(self) -> uuid.UUID:
        return sid("agent", self.n * 10 + 1)

    @property
    def email_agent_id(self) -> uuid.UUID:
        return sid("agent", self.n * 10 + 2)


def _pt(n: int, slug: str, name: str, tz: str, industry: str, created: int, domain: str,
        members: tuple[tuple[str, str, str], ...], calls: int, emails: int, drafts: float,
        status: str = "active") -> PlatformTenant:  # fmt: skip
    return PlatformTenant(n, sid("tenant", n), slug, name, tz, industry, status, created, domain, members, calls,
                          emails, drafts)  # fmt: skip


PLATFORM_TENANTS: tuple[PlatformTenant, ...] = (
    _pt(1, "brightside-dental", "Brightside Dental Group", "America/Chicago", "Dental clinics", 210,
        "brightsidedental.example", (("Laura", "Bennett", "tenant_owner"), ("Kevin", "Shaw", "tenant_admin"),
                                     ("Maria", "Lopez", "call_agent"), ("Tina", "Russell", "email_manager")),
        8, 250, 2.0),
    _pt(2, "summit-solar", "Summit Solar Installers", "America/Denver", "Solar installers", 160,
        "summitsolar.example", (("Greg", "Holloway", "tenant_owner"), ("Nina", "Patel", "campaign_manager"),
                                ("Derek", "Fleming", "call_agent"), ("Olivia", "Grant", "analyst"),
                                ("Sam", "Ortiz", "call_agent")), 12, 600, 1.5),
    _pt(3, "harbor-pine-realty", "Harbor & Pine Realty", "America/Los_Angeles", "Real estate", 120,
        "harborpine.example", (("Rachel", "Kim", "tenant_owner"), ("Tom", "Becker", "campaign_manager"),
                               ("Ashley", "Moreno", "email_manager")), 4, 800, 3.0),
    _pt(4, "northgate-legal", "Northgate Legal Partners", "America/New_York", "Law firms", 95,
        "northgatelegal.example", (("Daniel", "Weiss", "tenant_owner"), ("Monica", "Hale", "tenant_admin"),
                                   ("Peter", "Quinn", "call_agent")), 3, 80, 1.5),
    _pt(5, "evergreen-plumbing", "Evergreen Plumbing & Heating", "America/Chicago", "Home services", 75,
        "evergreenph.example", (("Carl", "Jensen", "tenant_owner"), ("Diane", "Marsh", "campaign_manager"),
                                ("Luis", "Romero", "call_agent"), ("Beth", "Coleman", "email_manager")),
        9, 300, 1.2),
    _pt(6, "lakeview-dentistry", "Lakeview Family Dentistry", "America/Chicago", "Dental clinics", 52,
        "lakeviewdental.example", (("Hannah", "Price", "tenant_owner"), ("Jared", "Fox", "call_agent")),
        3, 120, 0.8),
    _pt(7, "redwood-roofing", "Redwood Roofing & Gutters", "America/Los_Angeles", "Home services", 33,
        "redwoodroofing.example", (("Mike", "Dawson", "tenant_owner"), ("Erin", "Walsh", "campaign_manager"),
                                   ("Chris", "Lane", "call_agent")), 6, 150, 0.6),
    _pt(8, "clearview-insurance", "Clearview Insurance Agency", "America/Phoenix", "Insurance brokers", 140,
        "clearviewins.example", (("Paula", "Reyes", "tenant_owner"), ("Ian", "Murray", "analyst")), 0, 0, 0.0,
        status="suspended"),
)  # fmt: skip


def platform_user_id(t: PlatformTenant, k: int) -> uuid.UUID:
    return sid("user", t.n * 10 + k)


def platform_user_email(t: PlatformTenant, k: int) -> str:
    first, last, _ = t.members[k]
    return f"{first}.{last}@{t.domain}".lower()


# Globex (seed.py tenant) gets the same kind of synthetic usage history as the platform tenants.
GLOBEX_PROFILE = PlatformTenant(0, demo_id("tenant", 0), "globex", "Globex Dental", "America/Chicago",
                                "Dental clinics", "active", 300, "globex.test", (), 5, 200, 1.5)  # fmt: skip


@dataclass(slots=True)
class AiCall:
    """One AI Gateway request (metadata only, like `ai_requests`)."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    at: datetime
    caller: str
    agent_id: uuid.UUID | None
    purpose: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    status: str = "completed"
    deny_reason: str | None = None
    stop_reason: str | None = "end_turn"
    rounds: int = 1
    tool_calls: list[dict[str, str]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    input_chars: int = 0
    untrusted_blocks: int = 1

    @property
    def cost_micros(self) -> int:
        if self.status != "completed":
            return 0
        price_in, price_out = MODEL_PRICES[self.model]
        return (self.input_tokens * price_in + self.output_tokens * price_out) // 1_000_000

    @property
    def provider(self) -> str:
        return "anthropic"


# ai_gateway `ai_models` catalogue prices (micros per million tokens): input, output.
MODEL_PRICES = {"claude-haiku-5-5": (100_000, 500_000), "claude-sonnet-5-5": (2_000_000, 10_000_000)}


def acme_ai_calls(sc: Scenario) -> list[AiCall]:
    """Every AI request behind the Acme showcase: voice turns (Haiku), post-call analysis and email drafting
    (Sonnet), and email triage (Haiku). Deterministic, derived from the scenario."""
    rng = random.Random(SEED + 7)  # noqa: S311 - deterministic demo data
    out: list[AiCall] = []

    def add(**kw: Any) -> None:
        out.append(AiCall(id=sid("ai_request", len(out) + 1), tenant_id=TENANT.id, **kw))

    for c in sc.calls:
        if not c.has_session or c.answered_at is None:
            continue
        base = c.answered_at + timedelta(milliseconds=400)
        agent_turns = [s for s in c.segments if s.role == "agent"][1:]
        for k, seg in enumerate(agent_turns):
            tools: list[dict[str, str]] = []
            if c.booking is not None and ("booked for" in seg.text or "all set for" in seg.text):
                tools = [{"tool_id": "booking.find_slots", "status": "executed"},
                         {"tool_id": "booking.create_tentative", "status": "executed"},
                         {"tool_id": "booking.confirm", "status": "executed"}]  # fmt: skip
            elif "connect you" in seg.text or "transfer you" in seg.text:
                tools = [{"tool_id": "call.transfer_to_human", "status": "executed"}]
            elif "$" in seg.text and rng.random() < 0.7:
                tools = [{"tool_id": "knowledge.search", "status": "executed"}]
            add(at=base + timedelta(milliseconds=max(seg.started_ms - 700, 0)), caller="svc-voice-service",
                agent_id=VOICE_AGENT_ID, purpose="voice_turn", model="claude-haiku-5-5",
                input_tokens=1_250 + 90 * k + rng.randint(0, 160), output_tokens=len(seg.text) // 4 + 6,
                latency_ms=rng.randint(420, 980) + 350 * len(tools), rounds=1 + (1 if tools else 0),
                tool_calls=tools, input_chars=4_600 + 380 * k)  # fmt: skip
        end = c.ended_at or base
        add(at=end + timedelta(seconds=rng.randint(20, 35)), caller="svc-ai-agent-service", agent_id=VOICE_AGENT_ID,
            purpose="call_postprocess", model="claude-sonnet-5-5",
            input_tokens=900 + sum(len(s.text) for s in c.segments) // 4, output_tokens=rng.randint(120, 220),
            latency_ms=rng.randint(1_900, 3_800), input_chars=sum(len(s.text) for s in c.segments) + 600)  # fmt: skip
    for th in sc.threads:
        received = th.messages[0].received_at
        injection = th.scenario == "complaint" and th.n % 2 == 1
        add(at=received + timedelta(seconds=6), caller="svc-email-inbox-service", agent_id=EMAIL_AGENT_ID,
            purpose="classification", model="claude-haiku-5-5", input_tokens=700 + len(th.messages[0].text) // 4,
            output_tokens=rng.randint(40, 70), latency_ms=rng.randint(600, 1_300),
            input_chars=len(th.messages[0].text) + 900,
            flags=["prompt_injection_suspected"] if injection else [])  # fmt: skip
        if th.draft_versions:
            v1 = th.draft_versions[0]
            add(at=v1.created_at - timedelta(seconds=3), caller="svc-ai-agent-service", agent_id=EMAIL_AGENT_ID,
                purpose="email_draft", model="claude-sonnet-5-5", input_tokens=2_600 + rng.randint(0, 600),
                output_tokens=len(v1.body) // 4 + 20, latency_ms=rng.randint(3_200, 6_800), rounds=2,
                tool_calls=[{"tool_id": "knowledge.search", "status": "executed"}],
                input_chars=len(th.messages[0].text) + 7_800)  # fmt: skip
    return sorted(out, key=lambda a: a.at)


def tenant_daily(t: PlatformTenant, anchor: date) -> dict[tuple[date, str, str], int]:
    """Synthetic but internally consistent daily read-model counters for a non-Acme tenant (30 days)."""
    rng = random.Random(SEED + 1_000 + t.n)  # noqa: S311 - deterministic demo data
    out: dict[tuple[date, str, str], int] = {}
    created = anchor - timedelta(days=t.created_days_ago)
    suspended_from = anchor - timedelta(days=9) if t.status == "suspended" else None
    for i in range(WINDOW_DAYS):
        d = anchor - timedelta(days=WINDOW_DAYS - 1 - i)
        if d < created or (suspended_from and d >= suspended_from):
            continue
        weekday = d.weekday() < 5 and not is_holiday(d)
        growth = 0.8 + 0.4 * i / WINDOW_DAYS
        m: dict[str, int] = {}
        attempted = round(t.calls_per_day * growth * (1 if weekday else 0.18) * rng.uniform(0.75, 1.25))
        answered = round(attempted * rng.uniform(0.52, 0.68))
        completed = round(answered * rng.uniform(0.86, 0.94))
        duration = answered * rng.randint(95, 160)
        m.update({
            "calls.attempted": attempted, "calls.answered": answered, "calls.ended": attempted,
            "calls.completed": completed, "calls.failed": round((attempted - answered) * rng.uniform(0.05, 0.15)),
            "calls.opted_out": 1 if attempted and rng.random() < 0.15 else 0,
            "calls.qualified": round(answered * rng.uniform(0.3, 0.45)), "calls.duration_seconds": duration,
            "calls.cost_micros": attempted * 2_000 + duration * 650,
        })  # fmt: skip
        booked = round(answered * rng.uniform(0.18, 0.3))
        m.update({"bookings.created": booked, "bookings.confirmed": booked,
                  "bookings.canceled": 1 if booked > 3 and rng.random() < 0.3 else 0})  # fmt: skip
        sends = 0
        if t.emails_per_week and weekday and rng.random() < 0.35:
            sends = round(t.emails_per_week * rng.uniform(0.5, 1.1))
        bounced = round(sends * rng.uniform(0.01, 0.025))
        m.update({"emails.accepted": sends, "emails.delivered": sends - bounced, "emails.bounced": bounced,
                  "emails.complained": 1 if sends > 500 and rng.random() < 0.3 else 0,
                  "emails.unsubscribed": round(sends * rng.uniform(0.003, 0.012))})  # fmt: skip
        drafts = round(t.drafts_per_day * growth * (1 if weekday else 0.3) * rng.uniform(0.6, 1.4))
        approved = round(drafts * rng.uniform(0.78, 0.92))
        rejected = round((drafts - approved) * 0.6)
        m.update({"drafts.requested": drafts, "drafts.generated": drafts, "drafts.approved": approved,
                  "drafts.edited": round(approved * rng.uniform(0.15, 0.35)), "drafts.rejected": rejected,
                  "drafts.sent": approved})  # fmt: skip
        voice_req = answered * rng.randint(5, 8)
        req = {"voice_turn": voice_req, "call_postprocess": answered, "classification": drafts, "email_draft": drafts}
        tokens_in = {"voice_turn": 1_400, "call_postprocess": 1_500, "classification": 800, "email_draft": 2_900}
        tokens_out = {"voice_turn": 45, "call_postprocess": 170, "classification": 55, "email_draft": 280}
        model = {"voice_turn": "claude-haiku-5-5", "call_postprocess": "claude-sonnet-5-5",
                 "classification": "claude-haiku-5-5", "email_draft": "claude-sonnet-5-5"}  # fmt: skip
        latency = {"voice_turn": 700, "call_postprocess": 2_700, "classification": 900, "email_draft": 4_800}
        for purpose, n in req.items():
            if not n:
                continue
            p_in, p_out = MODEL_PRICES[model[purpose]]
            ti, to = n * tokens_in[purpose], n * tokens_out[purpose]
            for dim in ("", f"purpose:{purpose}"):
                for metric, value in (("ai.requests", n), ("ai.input_tokens", ti), ("ai.output_tokens", to),
                                      ("ai.cost_micros", (ti * p_in + to * p_out) // 1_000_000),
                                      ("ai.latency_ms", n * latency[purpose])):  # fmt: skip
                    out[(d, metric, dim)] = out.get((d, metric, dim), 0) + value
        new_leads = round(attempted * rng.uniform(0.08, 0.2)) + round(sends * 0.004)
        m["leads.created"] = new_leads
        for metric, value in m.items():
            out[(d, metric, "")] = value
    return out


def tenant_ai_usage(t: PlatformTenant, anchor: date) -> dict[tuple[str, str], tuple[int, int, int, int]]:
    """(period YYYY-MM, agent key) -> (requests, input tokens, output tokens, cost) from `tenant_daily`."""
    daily = tenant_daily(t, anchor)
    out: dict[tuple[str, str], list[int]] = {}
    for (d, metric, dim), value in daily.items():
        if not dim.startswith("purpose:") or metric == "ai.latency_ms":
            continue
        purpose = dim.split(":", 1)[1]
        agent = t.voice_agent_id if purpose in ("voice_turn", "call_postprocess") else t.email_agent_id
        row = out.setdefault((d.strftime("%Y-%m"), str(agent)), [0, 0, 0, 0])
        idx = ("ai.requests", "ai.input_tokens", "ai.output_tokens", "ai.cost_micros").index(metric)
        row[idx] += value
    return {k: (v[0], v[1], v[2], v[3]) for k, v in out.items()}

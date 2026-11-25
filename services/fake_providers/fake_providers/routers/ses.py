"""DEVELOPMENT/TEST ONLY: Amazon SES v2 outbound simulator.

* `POST /ses/v2/email/outbound-emails` accepts a raw-MIME send idempotently (`X-Idempotency-Key`: the same
  key always returns the same MessageId and is relayed once).
* The message is relayed to Mailpit over SMTP (`MAILPIT_SMTP_HOST`, default `mailpit:1025`).
* SES events are posted as signed SNS notifications (`fake_providers.sns.sign_sns`) to
  `EMAIL_EVENTS_WEBHOOK_URL`: normally `Delivery`; recipients at `bounce.example.test` get a permanent
  `Bounce`, recipients at `complaint.example.test` get `Delivery` then `Complaint`. Recipients at
  `reject.example.test` are refused synchronously with `MessageRejected`.
* `GET /ses/_idempotency/{key}` lets the email adapter reconcile an ambiguous send.
* Identity endpoints return deterministic DKIM tokens.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import hashlib
import json
import logging
import os
import smtplib
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from fake_providers.sns import _material, sign_sns
from nexus_core.webhooks import sns_string_to_sign

log = logging.getLogger(__name__)
router = APIRouter(prefix="/ses", tags=["fake-ses"])

DEFAULT_TOPIC = "arn:aws:sns:us-east-1:000000000000:nexus-ses-events"
BOUNCE_DOMAIN = "bounce.example.test"
COMPLAINT_DOMAIN = "complaint.example.test"
REJECT_DOMAIN = "reject.example.test"
MAX_RAW_BYTES = 10 * 1024 * 1024


@dataclass
class _State:
    by_key: dict[str, str] = field(default_factory=dict)
    sent: list[dict[str, Any]] = field(default_factory=list)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)


STATE = _State()


def reset() -> None:
    """Test helper: forget accepted sends and pending background deliveries."""
    STATE.by_key.clear()
    STATE.sent.clear()
    STATE.tasks.clear()


class _Destination(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ToAddresses: list[str] = Field(min_length=1, max_length=50)


class _Raw(BaseModel):
    Data: str


class _Content(BaseModel):
    Raw: _Raw


class _Tag(BaseModel):
    Name: str
    Value: str


class SendIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    FromEmailAddress: str
    Destination: _Destination
    Content: _Content
    EmailTags: list[_Tag] = []
    ConfigurationSetName: str | None = None


class IdentityIn(BaseModel):
    EmailIdentity: str = Field(min_length=3, max_length=253)


def dkim_tokens(domain: str) -> list[str]:
    return [hashlib.sha256(f"{domain}:{i}".encode()).hexdigest()[:32] for i in range(3)]


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"message": code}, status_code=status, headers={"x-amzn-ErrorType": code})


def ses_events(message_id: str, source: str, to: str, tags: dict[str, list[str]]) -> list[dict[str, Any]]:
    """The SES configuration-set events this recipient produces (Delivery / Bounce / Complaint)."""
    mail = {"timestamp": _now(), "source": source, "messageId": message_id, "destination": [to], "tags": tags}
    domain = to.rsplit("@", 1)[-1].lower()
    if domain == BOUNCE_DOMAIN:
        return [
            {
                "eventType": "Bounce",
                "mail": mail,
                "bounce": {
                    "bounceType": "Permanent",
                    "bounceSubType": "General",
                    "bouncedRecipients": [{"emailAddress": to, "action": "failed", "status": "5.1.1"}],
                    "timestamp": _now(),
                },
            }
        ]
    delivery = {
        "eventType": "Delivery",
        "mail": mail,
        "delivery": {"timestamp": _now(), "recipients": [to], "smtpResponse": "250 2.0.0 OK"},
    }
    if domain == COMPLAINT_DOMAIN:
        complaint = {
            "eventType": "Complaint",
            "mail": mail,
            "complaint": {
                "complainedRecipients": [{"emailAddress": to}],
                "complaintFeedbackType": "abuse",
                "timestamp": _now(),
            },
        }
        return [delivery, complaint]
    return [delivery]


def _relay_smtp(raw: bytes, source: str, to: str) -> None:
    hostport = os.environ.get("MAILPIT_SMTP_HOST", "mailpit:1025")
    if not hostport or hostport.lower() in ("off", "none"):
        return
    host, _, port = hostport.rpartition(":")
    with smtplib.SMTP(host or hostport, int(port) if port.isdigit() else 1025, timeout=10) as smtp:
        smtp.sendmail(source, [to], raw)


async def _post_events(events: list[dict[str, Any]]) -> None:
    url = os.environ.get("EMAIL_EVENTS_WEBHOOK_URL", "")
    if not url:
        return
    topic = os.environ.get("FAKE_SES_EVENTS_TOPIC_ARN", DEFAULT_TOPIC)
    delay = float(os.environ.get("FAKE_SES_EVENT_DELAY_SECONDS", "1.0"))
    await asyncio.sleep(delay)
    async with httpx.AsyncClient(timeout=10.0) as client:
        for event in events:
            envelope = sign_sns(topic, json.dumps(event))
            for attempt in range(4):  # SNS retries non-2xx deliveries
                try:
                    resp = await client.post(
                        url,
                        content=json.dumps(envelope),
                        headers={"Content-Type": "text/plain; charset=UTF-8", "x-amz-sns-message-type": "Notification"},
                    )
                    if resp.status_code < 300:
                        break
                except httpx.HTTPError:
                    log.warning("fake_ses_event_delivery_failed", extra={"attempt": attempt})
                await asyncio.sleep(2**attempt)


async def _side_effects(raw: bytes, source: str, to: str, events: list[dict[str, Any]]) -> None:
    try:
        await asyncio.to_thread(_relay_smtp, raw, source, to)
    except (OSError, smtplib.SMTPException):
        log.warning("fake_ses_mailpit_relay_failed")
    await _post_events(events)


def _spawn(coro: Any) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    STATE.tasks.add(task)
    task.add_done_callback(STATE.tasks.discard)


@router.post("/v2/email/outbound-emails")
async def send_email(
    body: SendIn, x_idempotency_key: str | None = Header(default=None, max_length=255)
) -> JSONResponse:
    if not x_idempotency_key:
        return _error(400, "BadRequestException")
    if x_idempotency_key in STATE.by_key:  # idempotent: same key -> same message, no second relay
        return JSONResponse({"MessageId": STATE.by_key[x_idempotency_key]})
    try:
        raw = base64.b64decode(body.Content.Raw.Data, validate=True)
    except ValueError:
        return _error(400, "BadRequestException")
    if len(raw) > MAX_RAW_BYTES:
        return _error(400, "MessageRejected")
    to = body.Destination.ToAddresses[0]
    if to.rsplit("@", 1)[-1].lower() == REJECT_DOMAIN:
        return _error(400, "MessageRejected")
    parsed = message_from_bytes(raw, policy=policy.default)
    source = parsed.get("From", body.FromEmailAddress)
    message_id = "0100" + hashlib.sha256(x_idempotency_key.encode()).hexdigest()[:56]
    STATE.by_key[x_idempotency_key] = message_id
    tags: dict[str, list[str]] = {}
    for tag in body.EmailTags:
        tags.setdefault(tag.Name, []).append(tag.Value)
    STATE.sent.append(
        {"message_id": message_id, "to": to, "subject": str(parsed.get("Subject", "")), "headers": dict(parsed.items())}
    )
    _spawn(_side_effects(raw, body.FromEmailAddress, to, ses_events(message_id, str(source), to, tags)))
    return JSONResponse({"MessageId": message_id})


@router.get("/_idempotency/{key}")
async def lookup(key: str) -> JSONResponse:
    message_id = STATE.by_key.get(key)
    if message_id is None:
        return _error(404, "NotFoundException")
    return JSONResponse({"MessageId": message_id})


@router.post("/v2/email/identities")
async def create_identity(body: IdentityIn) -> dict[str, Any]:
    domain = body.EmailIdentity.lower()
    return {
        "IdentityType": "DOMAIN",
        "VerifiedForSendingStatus": False,
        "DkimAttributes": {"Tokens": dkim_tokens(domain)},
    }


@router.get("/v2/email/identities/{domain}")
async def get_identity(domain: str) -> dict[str, Any]:
    verified = not domain.lower().startswith("unverified")
    return {
        "IdentityType": "DOMAIN",
        "VerifiedForSendingStatus": verified,
        "DkimAttributes": {"Status": "SUCCESS" if verified else "PENDING", "Tokens": dkim_tokens(domain.lower())},
    }


@router.post("/_subscribe", status_code=202)
async def simulate_subscription() -> dict[str, str]:
    """Sends a signed SubscriptionConfirmation to the events webhook (exercises the confirmation path)."""
    base = os.environ.get("FAKE_PROVIDERS_SELF_URL", "http://fake-providers:8000").rstrip("/")
    topic = os.environ.get("FAKE_SES_EVENTS_TOPIC_ARN", DEFAULT_TOPIC)
    envelope = sign_sns(topic, "You have chosen to subscribe to the topic.", message_type="SubscriptionConfirmation")
    envelope.pop("Signature")
    envelope["Token"] = hashlib.sha256(envelope["MessageId"].encode()).hexdigest()
    envelope["SubscribeURL"] = f"{base}/ses/_confirm?token={envelope['Token']}"
    # The confirmation fields are part of the string to sign, so sign again with them included.
    signature = _material()[0].sign(sns_string_to_sign(envelope), padding.PKCS1v15(), hashes.SHA256())
    envelope["Signature"] = base64.b64encode(signature).decode()
    url = os.environ.get("EMAIL_EVENTS_WEBHOOK_URL", "")
    if url:
        with contextlib.suppress(httpx.HTTPError):
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, content=json.dumps(envelope), headers={"Content-Type": "text/plain"})
    return {"status": "sent"}


@router.get("/_confirm")
async def confirm(token: str) -> dict[str, str]:
    return {"status": "confirmed", "token": token[:8]}

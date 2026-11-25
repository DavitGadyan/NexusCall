"""DEVELOPMENT/TEST ONLY: simulates Amazon SES inbound mail (receipt rule -> S3 object + SNS notification).

`POST /ses-inbound/simulate` builds an RFC 5322 message with the stdlib `email` package, stores it in the
`raw-mail` bucket under `inbound/<uuid>`, and posts a signed SNS `Received` notification to
`INBOUND_WEBHOOK_URL` (email-inbox-service `/webhooks/ses/inbound`).

`envelope_to` (the SMTP RCPT TO, which is what routes the mail to a tenant) defaults to `to`. Set it
separately to simulate spoofed `To:` headers. Arbitrary extra headers can be set to simulate hostile mail.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import uuid
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator

from fake_providers.sns import sign_sns
from nexus_core.config import ServiceSettings
from nexus_core.errors import Unprocessable
from nexus_core.storage import ObjectStore

router = APIRouter(prefix="/ses-inbound", tags=["fake-ses-inbound"])
BUCKET = "raw-mail"
_HEADER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,60}$")
_RESERVED = frozenset({"from", "to", "cc", "subject", "date", "content-type", "mime-version"})
_store: ObjectStore | None = None


def get_store() -> ObjectStore:
    global _store  # noqa: PLW0603 - one client per simulator process
    if _store is None:
        _store = ObjectStore.from_settings(ServiceSettings(service_name="fake-providers"))
    return _store


class AttachmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", pattern=r"^[a-z]+/[a-zA-Z0-9.+-]+$")
    content_b64: str = Field(max_length=20_000_000)


class SimulateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sender: str = Field(min_length=3, max_length=320)
    sender_name: str = Field(default="", max_length=200)
    to: list[str] = Field(min_length=1, max_length=20)
    envelope_to: list[str] | None = Field(default=None, max_length=20)
    cc: list[str] = Field(default_factory=list, max_length=20)
    subject: str = Field(default="", max_length=998)
    text: str = Field(default="", max_length=1_000_000)
    html: str | None = Field(default=None, max_length=2_000_000)
    headers: dict[str, str] = Field(default_factory=dict, max_length=30)
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=25)
    spam_verdict: Literal["PASS", "FAIL", "GRAY"] = "PASS"
    virus_verdict: Literal["PASS", "FAIL", "GRAY"] = "PASS"
    deliver: bool = True  # False: return the signed envelope instead of posting it

    @field_validator("headers")
    @classmethod
    def _headers(cls, value: dict[str, str]) -> dict[str, str]:
        for name, header_value in value.items():
            if not _HEADER_NAME.match(name) or name.lower() in _RESERVED:
                raise ValueError(f"header {name!r} is not allowed")
            if "\r" in header_value or "\n" in header_value:
                raise ValueError("header values must be single-line")
        return value


def build_mime(body: SimulateIn) -> tuple[bytes, str]:
    msg = EmailMessage()
    msg["From"] = f"{body.sender_name} <{body.sender}>" if body.sender_name else body.sender
    msg["To"] = ", ".join(body.to)
    if body.cc:
        msg["Cc"] = ", ".join(body.cc)
    msg["Subject"] = body.subject
    msg["Date"] = format_datetime(dt.datetime.now(dt.UTC))
    message_id = body.headers.get("Message-ID") or make_msgid(domain="sim.example.test")
    msg["Message-ID"] = message_id
    for name, value in body.headers.items():
        if name.lower() != "message-id":
            msg[name] = value
    msg.set_content(body.text or "")
    if body.html is not None:
        msg.add_alternative(body.html, subtype="html")
    for att in body.attachments:
        try:
            data = base64.b64decode(att.content_b64, validate=True)
        except ValueError as exc:
            raise Unprocessable("attachment content_b64 is not valid base64") from exc
        maintype, subtype = att.content_type.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=att.filename)
    return bytes(msg), message_id


def receipt_notification(body: SimulateIn, *, key: str, topic_arn: str, ses_message_id: str) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    recipients = body.envelope_to or body.to
    return {
        "notificationType": "Received",
        "mail": {
            "timestamp": now,
            "source": body.sender,
            "messageId": ses_message_id,
            "destination": recipients,
            "commonHeaders": {"from": [body.sender], "to": body.to, "subject": body.subject},
        },
        "receipt": {
            "timestamp": now,
            "recipients": recipients,
            "spamVerdict": {"status": body.spam_verdict},
            "virusVerdict": {"status": body.virus_verdict},
            "spfVerdict": {"status": "PASS"},
            "dkimVerdict": {"status": "PASS"},
            "dmarcVerdict": {"status": "PASS"},
            "action": {"type": "S3", "topicArn": topic_arn, "bucketName": BUCKET, "objectKey": key},
        },
    }


@router.post("/simulate")
async def simulate(body: SimulateIn, store: Annotated[ObjectStore, Depends(get_store)]) -> dict[str, Any]:
    raw, message_id = build_mime(body)
    key = f"inbound/{uuid.uuid4()}"
    await store.put(BUCKET, key, raw, content_type="message/rfc822")
    topic = os.environ.get("FAKE_SES_INBOUND_TOPIC_ARN", "arn:aws:sns:us-east-1:000000000000:nexus-inbound-dev")
    ses_message_id = uuid.uuid4().hex
    notification = receipt_notification(body, key=key, topic_arn=topic, ses_message_id=ses_message_id)
    envelope = sign_sns(topic, json.dumps(notification))
    result: dict[str, Any] = {
        "sns_message_id": envelope["MessageId"],
        "object_key": key,
        "message_id": message_id,
        "ses_message_id": ses_message_id,
    }
    if not body.deliver:
        return {**result, "envelope": envelope}
    url = os.environ.get("INBOUND_WEBHOOK_URL", "http://email-inbox-service:8000/webhooks/ses/inbound")
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        resp = await client.post(url, content=json.dumps(envelope), headers={"Content-Type": "text/plain"})
    return {**result, "webhook_status": resp.status_code}

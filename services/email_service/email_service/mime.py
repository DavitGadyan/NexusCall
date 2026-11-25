"""Raw MIME construction with the stdlib `email` package (sent through SES v2 `SendEmail` Content.Raw)."""

from __future__ import annotations

from email import policy
from email.message import EmailMessage
from email.utils import formataddr, formatdate

from email_service.security import UnsafeValue, has_crlf
from nexus_providers.email import OutboundEmail

# Only these extra headers may be set by callers (never arbitrary headers such as Bcc or Return-Path).
ALLOWED_EXTRA_HEADERS = (
    "Message-ID",
    "In-Reply-To",
    "References",
    "List-Unsubscribe",
    "List-Unsubscribe-Post",
    "Auto-Submitted",
)


def build_mime(message: OutboundEmail) -> bytes:
    for value in (message.subject, message.from_address, message.to_address, message.from_name or ""):
        if has_crlf(value):
            raise UnsafeValue("Header values must be a single line")
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = formataddr((message.from_name, message.from_address)) if message.from_name else message.from_address
    msg["To"] = message.to_address
    msg["Subject"] = message.subject
    msg["Date"] = formatdate(usegmt=True)
    if message.reply_to:
        if has_crlf(message.reply_to):
            raise UnsafeValue("Header values must be a single line")
        msg["Reply-To"] = message.reply_to
    for name, value in message.headers.items():
        if name not in ALLOWED_EXTRA_HEADERS:
            raise UnsafeValue(f"Header {name} is not allowed")
        if has_crlf(value):
            raise UnsafeValue("Header values must be a single line")
        msg[name] = value
    msg["X-Nexus-Category"] = message.category
    msg.set_content(message.text)
    if message.html:
        msg.add_alternative(message.html, subtype="html")
    return msg.as_bytes()

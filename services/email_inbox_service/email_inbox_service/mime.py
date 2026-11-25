"""Inbound MIME parsing and sanitization (stdlib `email` + bleach). Everything here is UNTRUSTED input.

* Header values never decide the tenant or mailbox (that comes from the SES envelope recipient).
* HTML is sanitized with a strict allowlist: no scripts, styles, forms, iframes or images (no tracking
  pixels, no zero-click exfiltration), and links limited to http(s)/mailto.
* Filenames are reduced to a safe display name; the stored object key never uses them.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr

import bleach

ALLOWED_TAGS = frozenset(
    {
        "a",
        "abbr",
        "b",
        "blockquote",
        "br",
        "code",
        "div",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "hr",
        "i",
        "li",
        "ol",
        "p",
        "pre",
        "span",
        "strong",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "u",
        "ul",
    }
)
ALLOWED_ATTRIBUTES = {"a": ["href", "title"], "td": ["colspan", "rowspan"], "th": ["colspan", "rowspan"]}
ALLOWED_PROTOCOLS = frozenset({"http", "https", "mailto"})
BLOCKED_EXTENSIONS = frozenset(
    {
        "exe",
        "dll",
        "com",
        "bat",
        "cmd",
        "scr",
        "pif",
        "vbs",
        "vbe",
        "js",
        "jse",
        "wsf",
        "wsh",
        "ps1",
        "psm1",
        "msi",
        "msp",
        "jar",
        "sh",
        "app",
        "dmg",
        "iso",
        "img",
        "lnk",
        "hta",
        "html",
        "htm",
        "svg",
        "xhtml",
        "docm",
        "xlsm",
        "pptm",
    }
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_INVISIBLE = re.compile("[\\u200b-\\u200f\\u202a-\\u202e\\u2060-\\u2064\\u2066-\\u2069\\ufeff\\U000e0000-\\U000e007f]")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_MSGID = re.compile(r"<[^<>\s]{1,900}>")
_ADDRESS = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
AUTO_PRECEDENCE = frozenset({"bulk", "junk", "list", "auto_reply"})


def normalize_address(value: str) -> str | None:
    """Lower-cased bare address, or None if it is not a plausible single address."""
    _, addr = parseaddr(value or "")
    addr = addr.strip().lower()
    return addr if _ADDRESS.match(addr) else None


def address_hash(address: str) -> str:
    return hashlib.sha256(address.strip().lower().encode()).hexdigest()


def clean_text(value: str, *, limit: int) -> str:
    """Strip control, ANSI and invisible characters (NFC), keep newlines and tabs, bound the length."""
    value = unicodedata.normalize("NFC", value or "")
    value = _ANSI.sub("", value)
    value = _INVISIBLE.sub("", value)
    value = _CONTROL.sub("", value).replace("\r\n", "\n").replace("\r", "\n")
    return value[:limit]


def clean_header(value: str | None, *, limit: int = 998) -> str:
    """Single-line header value: no CR/LF (header injection), no control characters."""
    return re.sub(r"\s+", " ", clean_text(str(value or ""), limit=limit * 2)).strip()[:limit]


def sanitize_html(html: str, *, limit: int) -> str:
    cleaned = bleach.clean(
        html[: limit * 2],
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
        strip_comments=True,
    )
    return clean_text(cleaned, limit=limit)


def html_to_text(html: str, *, limit: int) -> str:
    stripped = re.sub(r"(?is)<(script|style|head|title)[^>]*>.*?</\1>", " ", html[: limit * 2])
    stripped = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", stripped)
    text = bleach.clean(stripped, tags=set(), strip=True, strip_comments=True)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return clean_text(re.sub(r"\n{3,}", "\n\n", text), limit=limit)


def safe_filename(name: str | None) -> str:
    """Display-only filename: no path parts, no control/bidi characters, bounded length."""
    raw = clean_text(str(name or ""), limit=1000).replace("\\", "/").split("/")[-1]
    raw = re.sub(r"[^\w.\- ()\[\]+@,]", "_", raw).strip(" .")
    return (raw or "attachment")[:120]


def extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def message_ids(value: str | None) -> list[str]:
    """`<id>` tokens from Message-ID / In-Reply-To / References, bounded (at most 50)."""
    return _MSGID.findall(clean_header(value, limit=20_000))[-50:]


@dataclass(slots=True)
class ParsedAttachment:
    filename: str
    content_type: str
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(slots=True)
class ParsedEmail:
    message_id: str | None
    in_reply_to: str | None
    references: list[str]
    from_address: str | None
    from_name: str
    to_addresses: list[str]
    cc_addresses: list[str]
    subject: str
    text: str
    html: str | None
    attachments: list[ParsedAttachment] = field(default_factory=list)
    auto_flags: list[str] = field(default_factory=list)
    skipped_parts: int = 0


def _addresses(msg: EmailMessage, header: str) -> list[str]:
    values = [str(v) for v in msg.get_all(header, [])]
    out = []
    for _, addr in getaddresses(values)[:100]:
        norm = normalize_address(addr)
        if norm and norm not in out:
            out.append(norm)
    return out


def auto_reply_flags(msg: EmailMessage, from_address: str | None, mailbox_address: str | None) -> list[str]:
    """RFC 3834 auto-replies, bulk/list mail, bounces and mail loops: stored, but never drafted for."""
    flags: list[str] = []
    auto_submitted = clean_header(msg.get("Auto-Submitted")).lower()
    if auto_submitted and auto_submitted != "no":
        flags.append("auto_submitted")
    if clean_header(msg.get("Precedence")).lower() in AUTO_PRECEDENCE:
        flags.append("bulk_precedence")
    if any(msg.get(h) for h in ("X-Autoreply", "X-Autorespond", "X-Auto-Response-Suppress")):
        flags.append("auto_responder")
    if msg.get("List-Id") or msg.get("List-Unsubscribe"):
        flags.append("mailing_list")
    if from_address is None or from_address.split("@")[0] in ("mailer-daemon", "postmaster", "noreply", "no-reply"):
        flags.append("no_reply_sender")
    if msg.get_content_type() == "multipart/report":
        flags.append("delivery_report")
    if msg.get("X-Nexus-Loop") or (from_address is not None and from_address == mailbox_address):
        flags.append("loop")
    return flags


def parse_email(raw: bytes, *, max_text_chars: int, max_parts: int = 200) -> tuple[EmailMessage, ParsedEmail]:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    assert isinstance(msg, EmailMessage)
    from_header = str(msg.get("From", ""))
    from_name, _ = parseaddr(from_header)
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[ParsedAttachment] = []
    skipped = 0
    for index, part in enumerate(msg.walk()):
        if index >= max_parts:
            skipped += 1
            break
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        ctype = part.get_content_type().lower()
        filename = part.get_filename()
        if disposition == "attachment" or filename or ctype not in ("text/plain", "text/html"):
            try:
                payload = part.get_payload(decode=True) or b""
            except Exception:  # noqa: BLE001 - malformed encodings are skipped, never fatal
                skipped += 1
                continue
            attachments.append(ParsedAttachment(safe_filename(filename), ctype[:127], bytes(payload)))
            continue
        try:
            content = part.get_content()
        except Exception:  # noqa: BLE001
            skipped += 1
            continue
        (text_parts if ctype == "text/plain" else html_parts).append(str(content))
    html = "\n".join(html_parts) if html_parts else None
    text = "\n".join(text_parts) if text_parts else (html_to_text(html, limit=max_text_chars) if html else "")
    ids = message_ids(str(msg.get("Message-ID", "")))
    reply_to = message_ids(str(msg.get("In-Reply-To", "")))
    from_address = normalize_address(from_header)
    parsed = ParsedEmail(
        message_id=ids[0] if ids else None,
        in_reply_to=reply_to[0] if reply_to else None,
        references=message_ids(str(msg.get("References", ""))),
        from_address=from_address,
        from_name=clean_header(from_name, limit=200),
        to_addresses=_addresses(msg, "To"),
        cc_addresses=_addresses(msg, "Cc"),
        subject=clean_header(str(msg.get("Subject", "")), limit=998),
        text=clean_text(text, limit=max_text_chars),
        html=sanitize_html(html, limit=max_text_chars) if html else None,
        attachments=attachments,
        skipped_parts=skipped,
    )
    return msg, parsed


def reply_subject(subject: str) -> str:
    subject = clean_header(subject, limit=900)
    return subject if re.match(r"(?i)^re:", subject) else f"Re: {subject}".strip()

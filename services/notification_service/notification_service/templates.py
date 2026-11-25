"""Code-defined operational email templates (build.md §3.1N). Marketing mail belongs to email-service.

Rules enforced here, never by callers:
* only declared variables are accepted; required ones must be present;
* every value is a short single-line string without control characters (no header injection);
* every variable is HTML-escaped in the HTML part (the text part is plain text);
* link variables must point at `settings.public_base_url` (same scheme, host and port; no userinfo trick).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from string import Template
from urllib.parse import urlsplit

from nexus_core.errors import Unprocessable

MAX_VALUE_CHARS = 500
MAX_LINK_CHARS = 2000
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class EmailTemplate:
    key: str
    subject: str
    text: str
    html_body: str
    required: frozenset[str]
    optional: frozenset[str] = frozenset()
    links: frozenset[str] = frozenset({"link"})
    cta: str = "Open NexusCall"

    @property
    def allowed(self) -> frozenset[str]:
        return self.required | self.optional


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    subject: str
    text: str
    html: str


def _t(key: str, subject: str, text: str, html_body: str, required: set[str], optional: set[str] | None = None,
       cta: str = "Open NexusCall") -> EmailTemplate:  # fmt: skip
    return EmailTemplate(key, subject, text, html_body, frozenset(required), frozenset(optional or ()), cta=cta)


TEMPLATES: dict[str, EmailTemplate] = {
    t.key: t
    for t in (
        _t(
            "invitation",
            "You're invited to join $tenant_name on NexusCall",
            "$inviter invited you to join $tenant_name on NexusCall as: $roles.\n\n"
            "Accept the invitation: $link\n\nThis invitation expires at $expires_at. "
            "If you did not expect it, you can ignore this email.",
            "<p>$inviter invited you to join <strong>$tenant_name</strong> on NexusCall as: $roles.</p>"
            "<p>This invitation expires at $expires_at. If you did not expect it, you can ignore this email.</p>",
            {"tenant_name", "inviter", "roles", "link", "expires_at"},
            cta="Accept invitation",
        ),
        _t(
            "support_access_requested",
            "Support access to $tenant_name was requested",
            "The NexusCall platform team requested time-limited, read-only support access ($scope) to "
            "$tenant_name.\n\nReview the request: $link",
            "<p>The NexusCall platform team requested time-limited, read-only support access "
            "(<code>$scope</code>) to <strong>$tenant_name</strong>.</p>",
            {"tenant_name", "scope", "link"},
            {"expires_at", "ticket_ref"},
            cta="Review request",
        ),
        _t(
            "support_access_active",
            "Support access to $tenant_name is active",
            "Read-only support access ($scope) to $tenant_name is active until $expires_at. "
            "Every read is audited.\n\nDetails: $link",
            "<p>Read-only support access (<code>$scope</code>) to <strong>$tenant_name</strong> is active until "
            "$expires_at. Every read is audited.</p>",
            {"tenant_name", "scope", "expires_at", "link"},
        ),
        _t(
            "support_access_rejected",
            "Support access to $tenant_name was rejected",
            "A support access request for $tenant_name was rejected. No access was granted.\n\nDetails: $link",
            "<p>A support access request for <strong>$tenant_name</strong> was rejected. No access was granted.</p>",
            {"tenant_name", "link"},
            {"scope"},
        ),
        _t(
            "draft_ready",
            "An AI reply draft is ready for review",
            "An AI-generated reply draft is waiting for your review. Nothing is sent until an authorized "
            "reviewer approves it.\n\nReview: $link",
            "<p>An AI-generated reply draft is waiting for your review. Nothing is sent until an authorized "
            "reviewer approves it.</p>",
            {"link"},
            {"mailbox_name"},
            cta="Review draft",
        ),
        _t(
            "handoff_requested",
            "A caller asked for a human agent",
            "A caller asked to speak with a human agent.\n\nOpen the call: $link",
            "<p>A caller asked to speak with a human agent.</p>",
            {"link"},
            {"queue_name"},
            cta="Open call",
        ),
        _t(
            "generic",
            "$title",
            "$message\n\n$link",
            "<p>$message</p>",
            {"title", "message"},
            {"link"},
        ),
    )
}


def validate_link(value: str, public_base_url: str) -> str:
    """Accept only absolute links into the platform's own public origin (and under its base path)."""
    base = public_base_url.rstrip("/")
    if len(value) > MAX_LINK_CHARS or any(ch.isspace() for ch in value) or "\\" in value:
        raise Unprocessable("Link variables must be URLs under the platform's public base URL")
    if value != base and not value.startswith((base + "/", base + "?", base + "#")):
        raise Unprocessable("Link variables must be URLs under the platform's public base URL")
    got, want = urlsplit(value), urlsplit(base)
    if (got.scheme, got.netloc) != (want.scheme, want.netloc) or "@" in got.netloc:
        raise Unprocessable("Link variables must be URLs under the platform's public base URL")
    return value


def render(template_key: str, variables: dict[str, str], *, public_base_url: str) -> RenderedEmail:
    template = TEMPLATES.get(template_key)
    if template is None:
        raise Unprocessable("Unknown email template")
    unknown = set(variables) - template.allowed
    missing = template.required - set(variables)
    if unknown or missing:
        raise Unprocessable(
            "Template variables do not match the template",
            extra={"unknown": sorted(unknown), "missing": sorted(missing)},
        )
    clean: dict[str, str] = {}
    for name in template.allowed:
        value = variables.get(name, "")
        if not isinstance(value, str):
            raise Unprocessable(f"Variable '{name}' must be a string")
        limit = MAX_LINK_CHARS if name in template.links else MAX_VALUE_CHARS
        if len(value) > limit or _CONTROL.search(value):
            raise Unprocessable(f"Variable '{name}' is too long or contains control characters")
        if name in template.links and value:
            validate_link(value, public_base_url)
        clean[name] = value
    escaped = {k: html.escape(v, quote=True) for k, v in clean.items()}
    subject = Template(template.subject).substitute(clean)
    text = Template(template.text).substitute(clean).strip() + "\n\n-- \nNexusCall operational notification\n"
    body = Template(template.html_body).substitute(escaped)
    link = escaped.get("link", "")
    button = (
        f'<p><a href="{link}" style="display:inline-block;padding:10px 16px;background:#1d4ed8;color:#fff;'
        f'text-decoration:none;border-radius:6px">{html.escape(template.cta)}</a></p>'
        f'<p style="font-size:12px;color:#555">Or paste this link into your browser: {link}</p>'
        if link
        else ""
    )
    page = (
        '<!doctype html><html><body style="font-family:system-ui,sans-serif;font-size:14px;color:#111">'
        f"{body}{button}"
        '<hr><p style="font-size:12px;color:#555">NexusCall operational notification</p></body></html>'
    )
    return RenderedEmail(subject=subject[:200], text=text, html=page)

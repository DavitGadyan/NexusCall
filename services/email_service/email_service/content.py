"""Template safety: HTML sanitization (bleach), link policy, variable syntax and escaping (build.md §4
"Prevent open redirect and unsafe links in templates. Sanitize HTML; escape user-generated content").

Templates are validated when saved (problems -> 422) and sanitized before they are stored. Rendering only
substitutes allow-listed variables, HTML-escaped in the HTML part. No open or click tracking is ever added.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

import bleach

from email_service.security import has_crlf

ALLOWED_TAGS = frozenset(
    {
        "a", "abbr", "b", "blockquote", "br", "code", "div", "em", "h1", "h2", "h3", "h4", "hr", "i", "img",
        "li", "ol", "p", "pre", "small", "span", "strong", "sub", "sup", "table", "tbody", "td", "th", "thead",
        "tr", "u", "ul",
    }
)  # fmt: skip
ALLOWED_ATTRIBUTES = {
    "a": ["href", "title"],
    "img": ["src", "alt", "width", "height"],
    "table": ["width", "border", "cellpadding", "cellspacing", "align"],
    "td": ["colspan", "rowspan", "align", "width"],
    "th": ["colspan", "rowspan", "align", "width"],
    "p": ["align"],
    "div": ["align"],
}
ALLOWED_PROTOCOLS = frozenset({"http", "https"})
# Elements whose presence is an error in a saved template (their text would otherwise survive stripping).
FORBIDDEN_ELEMENTS = frozenset(
    {"script", "style", "iframe", "object", "embed", "form", "input", "button", "frame", "frameset", "meta",
     "link", "base", "svg", "math", "template", "noscript", "video", "audio", "applet"}
)  # fmt: skip
URL_ATTRIBUTES = frozenset(
    {"href", "src", "action", "formaction", "background", "poster", "cite", "srcset", "xlink:href", "data"}
)
# Public URL shorteners / redirectors: a template link must show its real destination.
REDIRECTOR_HOSTS = frozenset(
    {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly", "rebrand.ly", "cutt.ly",
     "shorturl.at", "tiny.cc", "rb.gy", "lnkd.in", "s.id", "v.gd", "t.ly", "bl.ink", "short.io"}
)  # fmt: skip
VARIABLES = {
    "first_name": "Recipient first name",
    "last_name": "Recipient last name",
    "full_name": "Recipient full name",
    "sender_name": "Sender display name",
    "unsubscribe_url": "One-click unsubscribe link (campaign templates only; added automatically if absent)",
}
_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,31})\s*\}\}")
_BRACES = re.compile(r"\{\{|\}\}")
_CONTROL = re.compile(r"[\x00-\x20\x7f]")
_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
UNSUBSCRIBE_VAR = "unsubscribe_url"


class TemplateError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def link_problem(url: str) -> str | None:
    """None when `url` is an acceptable absolute http(s) link to its real destination."""
    compact = _CONTROL.sub("", html_lib.unescape(url))
    if _VAR.fullmatch(compact) and _VAR.fullmatch(compact).group(1) == UNSUBSCRIBE_VAR:  # type: ignore[union-attr]
        return None
    if "{{" in compact or "}}" in compact:
        return "variables are not allowed inside links (except {{unsubscribe_url}})"
    parts = urlsplit(compact)
    if parts.scheme.lower() not in ALLOWED_PROTOCOLS:
        return "links must use http or https"
    if not parts.netloc or "@" in parts.netloc or "\\" in compact:
        return "links must be absolute and must not contain credentials"
    host = (parts.hostname or "").lower()
    if not host.isascii() or not _HOST.match(host):
        return "link host must be a plain DNS name"
    if any(label.startswith("xn--") for label in host.split(".")):
        return "internationalized (punycode) link hosts are not allowed (look-alike domains)"
    if host in REDIRECTOR_HOSTS:
        return "URL shorteners and redirectors are not allowed"
    tail = unquote(unquote(f"{parts.path}?{parts.query}#{parts.fragment}")).lower()
    if "http:" in tail or "https:" in tail or "//" in tail.lstrip("/"):
        return "links that redirect to another URL are not allowed"
    return None


class _Scanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in FORBIDDEN_ELEMENTS:
            self.problems.append(f"element <{tag}> is not allowed")
        for name, value in attrs:
            if name.startswith("on"):
                self.problems.append("event handler attributes are not allowed")
            if name in URL_ATTRIBUTES and value is not None:
                self.links.append(value)
                problem = link_problem(value)
                if problem:
                    self.problems.append(problem)

    handle_startendtag = handle_starttag


def scan_html(raw: str) -> list[str]:
    scanner = _Scanner()
    scanner.feed(raw)
    scanner.close()
    return sorted(set(scanner.problems))


def clean_html(raw: str) -> str:
    return bleach.clean(
        raw,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
        strip_comments=True,
    )


def html_to_text(value: str) -> str:
    value = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h[1-4]>|</li>|</tr>", "\n", value)
    value = bleach.clean(value, tags=set(), strip=True)
    text = html_lib.unescape(value)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.strip() for line in text.splitlines())).strip()


def variables_in(*parts: str) -> set[str]:
    return {m.group(1) for part in parts for m in _VAR.finditer(part)}


def _variable_problems(part: str, label: str, *, allow_unsubscribe: bool) -> list[str]:
    problems: list[str] = []
    leftovers = _BRACES.findall(_VAR.sub("", part))
    if leftovers:
        problems.append(f"{label}: malformed variable placeholder")
    for name in variables_in(part):
        if name not in VARIABLES:
            problems.append(f"{label}: unknown variable {{{{{name}}}}}")
        elif name == UNSUBSCRIBE_VAR and not allow_unsubscribe:
            problems.append(f"{label}: {{{{unsubscribe_url}}}} is only available in campaign templates")
    return problems


@dataclass(frozen=True, slots=True)
class CleanTemplate:
    subject: str
    html: str
    text: str
    variables: list[str]


def prepare_template(*, category: str, subject: str, html: str, text: str | None) -> CleanTemplate:
    """Validate and sanitize template content for storage. Raises TemplateError with every problem found."""
    problems: list[str] = []
    if has_crlf(subject):
        problems.append("subject: control characters (CR/LF) are not allowed")
    allow_unsub = category == "campaign"
    problems += _variable_problems(subject, "subject", allow_unsubscribe=False)
    problems += [f"html: {p}" for p in scan_html(html)]
    problems += _variable_problems(html, "html", allow_unsubscribe=allow_unsub)
    if text is not None:
        problems += _variable_problems(text, "text", allow_unsubscribe=allow_unsub)
    if problems:
        raise TemplateError(sorted(set(problems)))
    cleaned = clean_html(html)
    plain = text if text is not None and text.strip() else html_to_text(cleaned)
    return CleanTemplate(subject, cleaned, plain, sorted(variables_in(subject, cleaned, plain)))


def _free_attribute(tag: str, name: str, value: str) -> bool:
    if name not in ALLOWED_ATTRIBUTES.get(tag, []):
        return False
    if name in ("href", "src"):
        return "{{" not in value and link_problem(value) is None
    return True


def sanitize_free_html(raw: str) -> str:
    """For one-off content (replies through /internal/v1/send): strip anything unsafe instead of rejecting,
    including links that fail the link policy."""
    return bleach.clean(
        raw,
        tags=ALLOWED_TAGS,
        attributes=_free_attribute,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
        strip_comments=True,
    )


# ------------------------------------------------------------------------------------------------- rendering
def _plain(value: str) -> str:
    return _CONTROL.sub(" ", value).strip()[:200]


def render_part(template: str, values: dict[str, str], *, mode: str) -> str:
    """Substitute allow-listed variables. mode: html (escaped), text, subject (single line)."""

    def sub(match: re.Match[str]) -> str:
        name = match.group(1)
        raw = values.get(name, "")
        if name == UNSUBSCRIBE_VAR:
            return html_lib.escape(raw, quote=True) if mode == "html" else raw
        value = _plain(raw)
        return html_lib.escape(value, quote=True) if mode == "html" else value

    return _VAR.sub(sub, template)


@dataclass(frozen=True, slots=True)
class Rendered:
    subject: str
    html: str
    text: str


def render_template(
    *, category: str, subject: str, html: str, text: str, values: dict[str, str], unsubscribe_url: str | None
) -> Rendered:
    vals = dict(values)
    if unsubscribe_url:
        vals[UNSUBSCRIBE_VAR] = unsubscribe_url
    out_html = render_part(html, vals, mode="html")
    out_text = render_part(text, vals, mode="text")
    if category == "campaign" and unsubscribe_url:
        # A visible unsubscribe link is always present in marketing mail (in addition to List-Unsubscribe).
        safe = html_lib.escape(unsubscribe_url, quote=True)
        if UNSUBSCRIBE_VAR not in variables_in(html):
            out_html += f'<hr><p><small>Don\'t want these emails? <a href="{safe}">Unsubscribe</a>.</small></p>'
        if UNSUBSCRIBE_VAR not in variables_in(text):
            out_text += f"\n\n--\nUnsubscribe: {unsubscribe_url}\n"
    return Rendered(subject=render_part(subject, vals, mode="subject"), html=out_html, text=out_text)

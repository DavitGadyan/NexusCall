"""Pure unit tests: state machines, sanitization corpus, link policy, rendering, MIME, policy, DNS."""

from __future__ import annotations

import email
from datetime import UTC, datetime
from email import policy as email_policy

import pytest

from email_service.content import (
    TemplateError,
    clean_html,
    link_problem,
    prepare_template,
    render_template,
    sanitize_free_html,
)
from email_service.ingest import classify
from email_service.jobs import in_window
from email_service.mime import build_mime
from email_service.policy import blocks, seconds_until_next_day
from email_service.providers.dns import FakeDnsVerifier, RealDnsVerifier, dns_records
from email_service.security import (
    UnsafeValue,
    address_hash,
    has_crlf,
    mask_address,
    normalize_domain,
    valid_address,
    valid_message_id_list,
)
from email_service.sending import backoff
from email_service.state_machine import (
    CAMPAIGN_TRANSITIONS,
    MESSAGE_TRANSITIONS,
    InvalidTransition,
    campaign_next,
    message_can,
    message_next,
    sources_for,
)
from nexus_providers.email import OutboundEmail


# ------------------------------------------------------------------------------------------- state machines
@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("draft", "scheduled"),
        ("scheduled", "running"),
        ("running", "paused"),
        ("paused", "running"),
        ("paused", "scheduled"),
        ("running", "completed"),
        ("running", "canceled"),
        ("scheduled", "failed"),
    ],
)
def test_campaign_allowed_transitions(current, target):
    assert campaign_next(current, target) == target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("draft", "running"),
        ("completed", "running"),
        ("canceled", "scheduled"),
        ("failed", "running"),
        ("running", "scheduled"),
        ("draft", "paused"),
    ],
)
def test_campaign_forbidden_transitions(current, target):
    with pytest.raises(InvalidTransition):
        campaign_next(current, target)


def test_terminal_states_have_no_exits():
    assert all(not CAMPAIGN_TRANSITIONS[s] for s in ("completed", "canceled", "failed"))
    assert all(not MESSAGE_TRANSITIONS[s] for s in ("complained", "rejected", "failed"))


@pytest.mark.parametrize(
    ("current", "target", "ok"),
    [
        ("pending", "queued", True),
        ("queued", "sending", True),
        ("sending", "accepted", True),
        ("accepted", "delivered", True),
        ("delivered", "bounced", True),  # asynchronous bounce
        ("delivered", "complained", True),
        ("sending", "delivered", True),  # event before the worker recorded acceptance
        ("sending", "queued", True),  # deferral / reconcile-not-sent
        ("accepted", "queued", False),  # never resend something the provider accepted
        ("delivered", "accepted", False),  # never move backwards
        ("failed", "queued", False),
        ("bounced", "delivered", False),
        ("pending", "sending", False),
    ],
)
def test_message_transitions(current, target, ok):
    assert message_can(current, target) is ok
    if not ok:
        with pytest.raises(InvalidTransition):
            message_next(current, target)


def test_sources_for_guards_updates():
    assert "accepted" not in sources_for("queued")
    assert set(sources_for("accepted")) == {"sending"}
    assert "delivered" in sources_for("complained")


# ---------------------------------------------------------------------------------------- sanitization corpus
SAFE_LINK = "https://www.example.com/offer?id=7"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "jav&#x09;ascript:alert(1)",
        "  javascript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "//evil.test/path",
        "/relative/path",
        "mailto:x@y.test",
        "https://user:pass@example.com/",
        "https://good.example.com@evil.test/",
        "https://bit.ly/abc",
        "https://example.com/redirect?url=https://evil.test",
        "https://example.com/r?u=https%3A%2F%2Fevil.test",
        "https://example.com/r?u=https%253A%252F%252Fevil.test",
        "https://example.com/go/https://evil.test",
        "http://127.0.0.1/admin",
        "https://xn--pple-43d.com/",
        "https://exämple.com/",
        "https://example.com/{{first_name}}",
    ],
)
def test_bad_links_are_rejected(url):
    assert link_problem(url) is not None


@pytest.mark.parametrize(
    "url", [SAFE_LINK, "http://example.org/", "https://shop.example.co.uk/a/b#top", "{{ unsubscribe_url }}"]
)
def test_good_links_pass(url):
    assert link_problem(url) is None


@pytest.mark.parametrize(
    ("html", "problem"),
    [
        ("<script>alert(1)</script><p>x</p>", "element <script>"),
        ('<p onclick="steal()">x</p>', "event handler"),
        ('<img src="x" onerror="alert(1)">', "event handler"),
        ('<a href="javascript:alert(1)">x</a>', "http or https"),
        ('<iframe src="https://evil.test"></iframe>', "element <iframe>"),
        ('<form action="https://evil.test"><input name="pw"></form>', "element <form>"),
        ("<svg><script>alert(1)</script></svg>", "element <svg>"),
        ('<a href="https://bit.ly/x">x</a>', "shorteners"),
        ('<meta http-equiv="refresh" content="0;url=https://evil.test">', "element <meta>"),
        ("<style>body{background:url(javascript:x)}</style>", "element <style>"),
    ],
)
def test_template_corpus_rejects_unsafe_html(html, problem):
    with pytest.raises(TemplateError) as info:
        prepare_template(category="campaign", subject="Hi", html=html, text=None)
    assert any(problem in p for p in info.value.problems)


def test_template_sanitizer_strips_unknown_markup_and_attributes():
    clean = prepare_template(
        category="campaign",
        subject="Hello {{first_name}}",
        html='<div class="x" style="color:red"><blink>Hi</blink> <b>{{first_name}}</b> <a href="https://www.example.com/" target="_blank">go</a></div><!-- note -->',
        text=None,
    )
    assert "style=" not in clean.html and "class=" not in clean.html and "target=" not in clean.html
    assert "<blink>" not in clean.html and "<!--" not in clean.html
    assert '<a href="https://www.example.com/">go</a>' in clean.html
    assert clean.variables == ["first_name"] and "Hi" in clean.text


@pytest.mark.parametrize(
    ("subject", "html", "problem"),
    [
        ("Hi\r\nBcc: victim@x.test", "<p>x</p>", "CR/LF"),
        ("Hi {{password}}", "<p>x</p>", "unknown variable"),
        ("Hi", "<p>{{ first_name }</p>", "malformed"),
        ("Hi", "<p>{{unsubscribe_url}}</p>", None),  # allowed in campaigns
    ],
)
def test_template_variable_and_header_rules(subject, html, problem):
    if problem is None:
        prepare_template(category="campaign", subject=subject, html=html, text=None)
        with pytest.raises(TemplateError):  # ...but not in transactional templates
            prepare_template(category="transactional", subject=subject, html=html, text=None)
        return
    with pytest.raises(TemplateError) as info:
        prepare_template(category="campaign", subject=subject, html=html, text=None)
    assert any(problem in p for p in info.value.problems)


def test_rendering_escapes_variables_and_adds_unsubscribe():
    clean = prepare_template(category="campaign", subject="Hi {{first_name}}", html="<p>Hello {{first_name}}</p>",
                             text=None)  # fmt: skip
    out = render_template(
        category="campaign",
        subject=clean.subject,
        html=clean.html,
        text=clean.text,
        values={"first_name": '<script>alert("x")</script>\r\nBcc: a@b.test'},
        unsubscribe_url="https://app.test/u/tok",
    )
    assert "<script>" not in out.html and "&lt;script&gt;" in out.html
    assert "\r" not in out.subject and "\n" not in out.subject
    assert 'href="https://app.test/u/tok"' in out.html and "Unsubscribe" in out.html
    assert "https://app.test/u/tok" in out.text


def test_transactional_render_has_no_unsubscribe_footer():
    out = render_template(category="transactional", subject="s", html="<p>x</p>", text="x", values={},
                          unsubscribe_url=None)  # fmt: skip
    assert "nsubscribe" not in out.html + out.text


def test_free_html_is_stripped_not_rejected():
    out = sanitize_free_html(
        '<p onclick="x()">Hi</p><a href="javascript:alert(1)">bad</a><a href="https://www.example.com/">ok</a>'
        '<script>alert(1)</script><img src="https://bit.ly/p.png">'
    )
    assert "onclick" not in out and "javascript:" not in out and "<script>" not in out and "bit.ly" not in out
    assert '<a href="https://www.example.com/">ok</a>' in out
    assert "<a>bad</a>" in out or "bad" in out


def test_clean_html_drops_disallowed_protocols_as_defense_in_depth():
    assert "javascript" not in clean_html('<a href="javascript:alert(1)">x</a>')


# ------------------------------------------------------------------------------------------- headers / MIME
@pytest.mark.parametrize("value", ["a\r\nBcc: x@y.test", "a\nb", "a\rb", "a\x00b"])
def test_crlf_detection(value):
    assert has_crlf(value)


def _outbound(**over):
    base = dict(
        from_address="news@acme.test",
        from_name="Acme",
        to_address="ada@example.test",
        subject="Hello",
        text="Hi",
        html="<p>Hi</p>",
        headers={
            "Message-ID": "<abc@acme.test>",
            "List-Unsubscribe": "<https://app.test/u/t>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },  # fmt: skip
        category="campaign",
        idempotency_key="k",
    )
    base.update(over)
    return OutboundEmail(**base)


def test_mime_has_rfc8058_headers_and_both_parts():
    msg = email.message_from_bytes(build_mime(_outbound()), policy=email_policy.default)
    assert msg["List-Unsubscribe"] == "<https://app.test/u/t>"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert msg["Message-ID"] == "<abc@acme.test>" and msg["From"] == "Acme <news@acme.test>"
    assert {p.get_content_type() for p in msg.walk()} >= {"text/plain", "text/html"}


@pytest.mark.parametrize(
    "over",
    [
        {"subject": "Hi\r\nBcc: victim@x.test"},
        {"from_name": "Acme\nBcc: x@y.test"},
        {"headers": {"Bcc": "victim@x.test"}},
        {"headers": {"References": "<a@b>\r\nBcc: x@y.test"}},
        {"reply_to": "a@b.test\nBcc: c@d.test"},
    ],
)
def test_mime_refuses_header_injection(over):
    with pytest.raises((UnsafeValue, ValueError)):
        build_mime(_outbound(**over))


def test_message_id_lists():
    assert valid_message_id_list("<a@b.test> <c@d.test>")
    assert not valid_message_id_list("<a@b.test>\r\nBcc: x")
    assert not valid_message_id_list("not-an-id")


# ------------------------------------------------------------------------------------------- misc helpers
def test_address_helpers():
    assert address_hash("  Ada@Example.TEST ") == address_hash("ada@example.test")
    assert len(address_hash("a@b.test")) == 64
    assert valid_address("ada.l+news@example.test") and not valid_address("ada@") and not valid_address("a b@c.d")
    assert mask_address("ada@example.test") == "a***@example.test"
    assert normalize_domain("Mail.Example.TEST.") == "mail.example.test"
    with pytest.raises(UnsafeValue):
        normalize_domain("exa mple.com")


def test_marketing_and_transactional_streams_are_separate():
    assert blocks("campaign", ["opt_out"])
    assert not blocks("reply", ["opt_out"]) and not blocks("transactional", ["opt_out"])
    assert blocks("reply", ["bounce"]) and blocks("transactional", ["complaint", "opt_out"])
    assert not blocks("campaign", [])


def test_window_in_campaign_timezone():
    window = {"start": "09:00", "end": "17:00", "days": ["mon", "tue", "wed", "thu", "fri"]}
    monday_14_utc = datetime(2026, 11, 23, 14, 0, tzinfo=UTC)  # 09:00 in New York
    assert in_window(window, "America/New_York", monday_14_utc)
    assert not in_window(window, "UTC", datetime(2026, 11, 23, 18, 0, tzinfo=UTC))
    assert not in_window(window, "UTC", datetime(2026, 11, 22, 12, 0, tzinfo=UTC))  # Sunday
    assert in_window(None, "UTC", monday_14_utc)


def test_backoff_is_bounded_with_jitter():
    values = [backoff(n, base=30, cap=1800) for n in range(1, 12)]
    assert all(v <= 1800 * 1.2 for v in values) and values[0] < values[3]


def test_quota_retry_waits_until_next_utc_day():
    assert 0 < seconds_until_next_day(datetime(2026, 11, 25, 23, 59, tzinfo=UTC)) <= 120


@pytest.mark.parametrize(
    ("event", "target"),
    [
        ({"eventType": "Delivery"}, "delivered"),
        ({"eventType": "Send"}, "accepted"),
        ({"eventType": "Bounce", "bounce": {"bounceType": "Permanent"}}, "bounced"),
        ({"eventType": "Bounce", "bounce": {"bounceType": "Transient"}}, None),
        ({"eventType": "Complaint"}, "complained"),
        ({"eventType": "Reject"}, "rejected"),
        ({"notificationType": "Delivery"}, "delivered"),
        ({"eventType": "Open"}, None),  # opens and clicks are not tracked
        ({"eventType": "Click"}, None),
    ],
)
def test_event_mapping(event, target):
    assert classify(event)[1] == target


# ------------------------------------------------------------------------------------------------ DNS
class _Answer(list):
    pass


class _Txt:
    def __init__(self, value: str) -> None:
        self.strings = [value.encode()]


class _Cname:
    def __init__(self, value: str) -> None:
        self.target = value + "."


class FakeResolver:
    def __init__(self, records: dict[tuple[str, str], list]) -> None:
        self.records = records
        self.queries: list[tuple[str, str]] = []

    async def resolve(self, name: str, rdtype: str):
        import dns.resolver

        self.queries.append((name, rdtype))
        if (name, rdtype) not in self.records:
            raise dns.resolver.NXDOMAIN
        return _Answer(self.records[(name, rdtype)])


async def test_real_dns_verifier_checks_dkim_spf_dmarc():
    tokens = ["tok1", "tok2"]
    resolver = FakeResolver(
        {
            ("tok1._domainkey.acme.test", "CNAME"): [_Cname("tok1.dkim.amazonses.com")],
            ("tok2._domainkey.acme.test", "CNAME"): [_Cname("tok2.dkim.amazonses.com")],
            ("acme.test", "TXT"): [_Txt("v=spf1 include:amazonses.com ~all")],
            ("_dmarc.acme.test", "TXT"): [_Txt("v=DMARC1; p=none")],
        }
    )
    result = await RealDnsVerifier(resolver=resolver).check("acme.test", tokens)
    assert (result.dkim, result.spf, result.dmarc, result.verified) == ("pass", "pass", "pass", True)
    partial = FakeResolver({("tok1._domainkey.acme.test", "CNAME"): [_Cname("tok1.dkim.amazonses.com")]})
    result = await RealDnsVerifier(resolver=partial).check("acme.test", tokens)
    assert (result.dkim, result.spf, result.dmarc, result.verified) == ("fail", "fail", "fail", False)
    wrong = FakeResolver({("tok1._domainkey.acme.test", "CNAME"): [_Cname("tok1.dkim.evil.test")]})
    assert (await RealDnsVerifier(resolver=wrong).check("acme.test", ["tok1"])).dkim == "fail"


async def test_fake_dns_is_deterministic():
    fake = FakeDnsVerifier()
    assert (await fake.check("acme.test", [])).verified
    assert not (await fake.check("unverified-co.test", [])).verified
    assert (await fake.check("nodmarc.test", [])).dmarc == "fail"


def test_dns_records_shown_to_tenant():
    records = dns_records("acme.test", ["t1"])
    assert {
        "type": "CNAME",
        "name": "t1._domainkey.acme.test",
        "value": "t1.dkim.amazonses.com",
        "purpose": "dkim",
    } in records
    assert {r["purpose"] for r in records} == {"dkim", "spf", "dmarc"}

"""Unit tests (no Docker): MIME parsing and sanitization, receipt validation, the clamd client, the drafting
output contract and state rules."""

from __future__ import annotations

import asyncio
import json
import struct
import uuid

import pytest
from inbox_support import EICAR, draft_result
from pydantic import ValidationError

from email_inbox_service.drafting import ESCALATE_INTENTS, DraftingResult, context_blocks
from email_inbox_service.inbound import InvalidNotification, parse_envelope, parse_receipt
from email_inbox_service.mime import (
    auto_reply_flags,
    clean_header,
    message_ids,
    normalize_address,
    parse_email,
    reply_subject,
    safe_filename,
    sanitize_html,
)
from email_inbox_service.scanner import ScannerUnavailable, scan_bytes
from email_inbox_service.sending import idempotency_key
from email_inbox_service.settings import Settings
from fake_providers.routers.ses_inbound import SimulateIn, build_mime

SETTINGS = Settings(env="test")


def test_sanitize_html_removes_active_content_and_tracking():
    dirty = (
        '<p onclick="x()">Hi</p><script>alert(1)</script><img src="https://t.example/p.gif">'
        '<a href="javascript:alert(1)">x</a><a href="https://ok.example">ok</a><iframe src="https://e"></iframe>'
        "<style>body{}</style><form action=https://e><input></form><!-- hidden instruction -->"
    )
    clean = sanitize_html(dirty, limit=10_000)
    for needle in ("<script", "onclick", "<img", "javascript:", "<iframe", "<style", "<form", "<!--"):
        assert needle not in clean
    assert '<a href="https://ok.example">ok</a>' in clean


def test_filenames_headers_and_ids_are_normalized():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("C:\\Users\\a\\evil.exe") == "evil.exe"
    assert safe_filename("in\u202evoice\u200b<script>.pdf") == "invoice_script_.pdf"
    assert safe_filename("") == "attachment" and len(safe_filename("a" * 500)) == 120
    assert clean_header("Hello\r\nBcc: x@evil.example") == "Hello Bcc: x@evil.example"
    assert message_ids("<a@x> junk <b@y>") == ["<a@x>", "<b@y>"]
    assert normalize_address("Name <USER@Example.TEST>") == "user@example.test"
    assert normalize_address("not an address") is None and normalize_address("a@b@c") is None
    assert reply_subject("Question") == "Re: Question" and reply_subject("RE: Question") == "RE: Question"


def test_parse_email_extracts_parts_and_flags():
    raw, _ = build_mime(
        SimulateIn(
            sender="c@example.test",
            to=["s@acme.example.test"],
            subject="Hi",
            text="Plain body",
            html="<p>HTML body</p>",
            headers={"Auto-Submitted": "auto-replied", "In-Reply-To": "<x@y>"},
            attachments=[{"filename": "f.pdf", "content_type": "application/pdf", "content_b64": "JVBERi0="}],
        )
    )
    msg, parsed = parse_email(raw, max_text_chars=1000)
    assert parsed.text.strip() == "Plain body" and "HTML body" in (parsed.html or "")
    assert parsed.in_reply_to == "<x@y>" and parsed.attachments[0].filename == "f.pdf"
    assert "auto_submitted" in auto_reply_flags(msg, parsed.from_address, "s@acme.example.test")
    assert "loop" in auto_reply_flags(msg, "s@acme.example.test", "s@acme.example.test")


def _receipt(**action):
    base = {"type": "S3", "bucketName": "raw-mail", "objectKey": "inbound/abc"}
    return json.dumps(
        {"notificationType": "Received", "receipt": {"recipients": ["A@x.test"], "action": {**base, **action}}}
    )


def test_receipt_validation():
    recipients, bucket, key, _, _ = parse_receipt(SETTINGS, _receipt())
    assert recipients == ["a@x.test"] and (bucket, key) == ("raw-mail", "inbound/abc")
    for bad in (
        _receipt(bucketName="other"),
        _receipt(objectKey="tenant/x"),
        _receipt(objectKey="inbound/../x"),
        _receipt(type="Lambda"),
        "not json",
        json.dumps({"notificationType": "Bounce"}),
    ):
        with pytest.raises(InvalidNotification):
            parse_receipt(SETTINGS, bad)
    with pytest.raises(InvalidNotification):
        parse_envelope(b'{"Type": 1}')


async def _clamd(reply: bytes) -> tuple[asyncio.AbstractServer, int]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\0")
        while True:
            size = struct.unpack("!L", await reader.readexactly(4))[0]
            if size == 0:
                break
            await reader.readexactly(size)
        writer.write(reply)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_clamd_client_results():
    for reply, expected in (
        (b"stream: OK\0", (False, None)),
        (b"stream: Eicar-Test-Signature FOUND\0", (True, "Eicar-Test-Signature")),
    ):
        server, port = await _clamd(reply)
        async with server:
            result = await scan_bytes("127.0.0.1", port, EICAR * 2000, timeout_seconds=5)
        assert (result.infected, result.signature) == expected
    server, port = await _clamd(b"INSTREAM size limit exceeded. ERROR\0")
    async with server:
        with pytest.raises(ScannerUnavailable):
            await scan_bytes("127.0.0.1", port, b"x", timeout_seconds=5)
    with pytest.raises(ScannerUnavailable):
        await scan_bytes(None, 3310, b"x")
    with pytest.raises(ScannerUnavailable):
        await scan_bytes("127.0.0.1", 1, b"x", timeout_seconds=1)


def test_drafting_output_contract_is_strict():
    ok = DraftingResult.model_validate(draft_result({}))
    assert ok.quality.confidence == 0.82
    for broken in ({"draft_text": None}, {"sensitive": "maybe"}, {"run_id": "x"}, {"quality": {"confidence": 7}}):
        with pytest.raises(ValidationError):
            DraftingResult.model_validate({**draft_result({}), **broken})
    assert {"complaint", "legal", "privacy_request", "unsubscribe"} <= ESCALATE_INTENTS


def test_context_blocks_are_untrusted_and_bounded():
    history = [
        {
            "text_body": "x" * 50_000,
            "direction": "inbound",
            "from_name": "A",
            "from_address": "a@x.test",
            "subject": "S",
        }
    ]
    blocks = context_blocks(SETTINGS, history, ["evil<script>.pdf"] * 50)
    assert all(b["trust"] == "untrusted" for b in blocks)
    assert len(blocks[0]["text"]) < SETTINGS.draft_context_chars_per_message + 200
    assert blocks[1]["text"].count("\n- ") == 20


def test_idempotency_key_shape():
    draft_id = uuid.uuid4()
    assert idempotency_key(draft_id, 3) == f"draft:{draft_id}:v3"

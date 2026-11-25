import json
import logging

from nexus_core.logging import JsonFormatter, redact, redact_text


def test_redacts_sensitive_keys_and_patterns():
    data = {
        "password": "hunter2",
        "nested": {"refresh_token": "abc"},
        "note": "call +1 415 555 0100",
        "who": "contact bob@example.com",
        "auth": "Bearer abc.def.ghi",
    }
    out = redact(data)
    assert out["password"] == "[REDACTED]"
    assert out["nested"]["refresh_token"] == "[REDACTED]"
    assert "[PHONE]" in out["note"]
    assert "[EMAIL]" in out["who"]
    assert "abc.def" not in json.dumps(out)


def test_jwt_and_vault_token_masked():
    s = redact_text(
        "token eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlc2lnbmF0dXJl"
        " and hvs.CAESIJ1234567890abcdefghij"
    )
    assert "eyJ" not in s and "hvs." not in s


def test_formatter_redacts_extras():
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "sending to ann@example.com", (), None)
    rec.api_key = "sk-live-1234567890abcdefgh"
    rec.tenant = "t1"
    line = json.loads(JsonFormatter("svc", "test").format(rec))
    assert line["api_key"] == "[REDACTED]"
    assert "[EMAIL]" in line["msg"]
    assert line["service"] == "svc"

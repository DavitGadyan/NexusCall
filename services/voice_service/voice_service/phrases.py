"""Deterministic caller-intent detection for safety-relevant requests (OWASP LLM01/LLM06 brief).

Opt-out, human handoff and "do not record" are decided HERE, in code, before any model sees the turn. A model
can never cancel, delay or reinterpret an opt-out. Matching runs on normalized text (NFKC, lowercase, no
punctuation, collapsed spaces) so spacing, case and look-alike characters do not defeat it.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

Intent = Literal["opt_out", "handoff", "stop_recording"]

_OPT_OUT = [
    r"\bstop (calling|contacting|phoning|ringing) me\b",
    r"\b(do not|dont|never) (call|contact|phone|ring) (me|this number)\b",
    r"\b(remove|take) (me|my number) (off|from)\b",
    r"\b(put|add) me (on|to) (your|the) (do not call|dnc)\b",
    r"\bdo not call list\b",
    r"\b(opt|opting) out\b",
    r"\bunsubscribe\b",
    r"\bno more calls\b",
    r"\bstop the calls\b",
    r"\b(i am|im) not interested (stop|dont call)\b",
]
_HANDOFF = [
    r"\b(speak|talk) (to|with) (a |an )?(human|person|agent|representative|real person|someone|operator|manager)\b",
    r"\b(transfer|connect|put) me (to|through to|with) (a |an )?(human|person|agent|representative|someone)\b",
    r"\b(human|real person|representative|operator) please\b",
]
_STOP_RECORDING = [
    r"\b(do not|dont) record\b",
    r"\bstop (the )?recording\b",
    r"\bi do not consent to (being )?record",
    r"\bi dont consent to (being )?record",
]
_COMPILED: list[tuple[Intent, re.Pattern[str]]] = [
    *[("opt_out", re.compile(p)) for p in _OPT_OUT],
    *[("stop_recording", re.compile(p)) for p in _STOP_RECORDING],
    *[("handoff", re.compile(p)) for p in _HANDOFF],
]


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")  # control/format chars
    text = text.replace("'", "").replace("’", "")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def detect_intent(text: str) -> Intent | None:
    """Opt-out wins over everything else (it is checked first)."""
    norm = normalize(text)
    for intent, pattern in _COMPILED:
        if pattern.search(norm):
            return intent
    return None

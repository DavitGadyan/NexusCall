"""Pure domain rules: classifications, tool allowlists, file validation, text hygiene, PII flags, chunking.

Nothing here talks to the network or the database, so every rule is unit-testable."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

# ------------------------------------------------------------------------------------------- classifications
CLASS_RANK = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
CLASSIFICATIONS = tuple(CLASS_RANK)


def classes_up_to(cap: str) -> list[str]:
    """Classifications at or below `cap`. An unknown cap fails closed to `public` only."""
    limit = CLASS_RANK.get(cap, 0)
    return [c for c, rank in CLASS_RANK.items() if rank <= limit]


def max_class(*values: str) -> str:
    return max(values, key=lambda v: CLASS_RANK.get(v, 3))


def source_requires_approval(origin: str, classification: str, requested: bool) -> bool:
    """High-risk sources always need human approval. Clients may make a source stricter, never looser."""
    return requested or origin == "external" or CLASS_RANK.get(classification, 3) >= CLASS_RANK["confidential"]


# ------------------------------------------------------------------------------------------- agents and tools
# Narrow, per-kind tool sets (OWASP LLM01/LLM03). Email drafting never uses tools; voice agents never get the
# approval-gated `booking.confirm` or any privileged tool. The real grant still lives in mcp-registry.
VOICE_TOOLS = frozenset(
    {
        "knowledge.search",
        "booking.find_slots",
        "booking.create_tentative",
        "crm.get_contact",
        "crm.add_note",
        "call.transfer_to_human",
    }
)
EMAIL_TOOLS: frozenset[str] = frozenset()
TOOLS_BY_KIND = {"voice": VOICE_TOOLS, "email": EMAIL_TOOLS}

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(hvs|hvb)\.[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|xox[abpr]-[A-Za-z0-9-]{10,})"),
    re.compile(r"(?i)\b(password|passwd|api[_-]?key|secret|token)\s*[:=]\s*\S{6,}"),
    re.compile(r"(?i)\b(postgres(ql)?|mysql|redis|amqp|mongodb)(\+\w+)?://[^\s:/]+:[^\s@/]+@"),
)


def contains_credentials(text: str) -> bool:
    """Credentials must never enter a prompt (build.md §24.6 rule 6; OWASP LLM08)."""
    return any(p.search(text) for p in _SECRET_PATTERNS)


def invalid_tools(kind: str, tool_ids: list[str]) -> list[str]:
    allowed = TOOLS_BY_KIND.get(kind, frozenset())
    return sorted(t for t in set(tool_ids) if t not in allowed)


# ------------------------------------------------------------------------------------------- file validation
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_TYPES = {".txt": "text/plain", ".md": "text/markdown", ".pdf": "application/pdf"}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")


class FileRejected(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def safe_filename(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _SAFE_NAME.sub("_", unicodedata.normalize("NFKC", base)).strip(" .")
    return cleaned[:200] or "document"


def validate_upload(filename: str, data: bytes, *, max_bytes: int = MAX_UPLOAD_BYTES) -> str:
    """Type and size validation by extension AND content (magic bytes / UTF-8). Returns the content type."""
    if not data:
        raise FileRejected("empty_file")
    if len(data) > max_bytes:
        raise FileRejected("file_too_large")
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    content_type = ALLOWED_TYPES.get(ext)
    if content_type is None:
        raise FileRejected("unsupported_file_type")
    if content_type == "application/pdf":
        if not data.startswith(b"%PDF-"):
            raise FileRejected("content_does_not_match_type")
        return content_type
    if data.startswith(b"%PDF-") or b"\x00" in data:
        raise FileRejected("content_does_not_match_type")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileRejected("text_not_utf8") from exc
    return content_type


def sha256_hex(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


# ------------------------------------------------------------------------------------------- malware scan
# Built-in signature scan (optional external AV can replace it). EICAR proves the hook works end to end;
# active PDF content is not executed by our text extraction, but it marks the document for human review.
_EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
_PDF_ACTIVE = re.compile(rb"/(JavaScript|JS|Launch|EmbeddedFile|RichMedia|XFA)\b")


@dataclass(frozen=True, slots=True)
class ScanResult:
    status: str  # clean | suspicious
    malware: bool = False
    reasons: tuple[str, ...] = ()


def scan_bytes(data: bytes, content_type: str) -> ScanResult:
    if _EICAR in data:
        return ScanResult("suspicious", True, ("malware_signature",))
    if content_type == "application/pdf" and _PDF_ACTIVE.search(data):
        return ScanResult("suspicious", False, ("active_pdf_content",))
    return ScanResult("clean")


# ------------------------------------------------------------------------------------------- text hygiene
_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass(slots=True)
class CleanText:
    text: str
    hidden_removed: int = 0


def clean_text(raw: str) -> CleanText:
    """NFKC, strip ANSI/control and invisible characters (they can hide instructions, LLM01/LLM05)."""
    text = unicodedata.normalize("NFKC", raw).replace("\r\n", "\n").replace("\r", "\n")
    text = _ANSI.sub("", text)
    hidden = len(_INVISIBLE.findall(text))
    text = _INVISIBLE.sub("", text)
    text = _CONTROL.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return CleanText(text, hidden)


# ------------------------------------------------------------------------------------------- PII classification
_PII = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "phone": re.compile(r"(?<!\w)\+?\d[\d\s().-]{8,}\d(?!\w)"),
    "us_ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "payment_card": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
}


def pii_kinds(text: str) -> list[str]:
    return [name for name, pattern in _PII.items() if pattern.search(text)]


# ------------------------------------------------------------------------------------------- chunking
@dataclass(slots=True)
class Chunk:
    ordinal: int
    text: str
    sha256: str = field(default="")


def chunk_text(text: str, *, size: int = 1200, overlap: int = 150, max_chunks: int = 2000) -> list[Chunk]:
    """Paragraph-aware chunks of at most `size` characters with a small overlap."""
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    pieces: list[str] = []
    for paragraph in paragraphs:
        rest = paragraph
        while len(rest) > size:
            cut = rest.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            pieces.append(rest[:cut].strip())
            rest = rest[max(cut - overlap, 1) :].strip()
        if rest:
            pieces.append(rest)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + 2 + len(piece) > size:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            tail = tail[tail.find(" ") + 1 :] if " " in tail else ""
            current = f"{tail} {piece}".strip() if tail and len(tail) + 1 + len(piece) <= size else piece
        else:
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    if len(chunks) > max_chunks:
        raise ValueError("too_many_chunks")
    return [Chunk(i, c, sha256_hex(c)) for i, c in enumerate(chunks)]


# ------------------------------------------------------------------------------------------- grounding
_FACTUAL_HINT = re.compile(
    r"(\$\s?\d|\d+(\.\d+)?\s?(%|usd|dollars|eur|minutes?|hours?|days?|weeks?)|\b\d{1,2}(:\d{2})?\s?(am|pm)\b|"
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|\b(price|cost|fee|refund|warranty|open|closed)\b)",
    re.I,
)


def looks_factual(text: str) -> bool:
    """Deterministic check that a draft states facts (prices, times, policies). Used so a manipulated model
    cannot avoid the `ungrounded` flag just by claiming its answer is not factual (OWASP LLM07/LLM09)."""
    return bool(_FACTUAL_HINT.search(text))


def ngram_overlap(text: str, secret: str, n: int = 8) -> bool:
    """True when `text` repeats any `n` consecutive words of `secret` (hidden-context leak check for evals)."""
    words = re.findall(r"\w+", secret.lower())
    if len(words) < n:
        return len(words) >= 4 and " ".join(words) in " ".join(re.findall(r"\w+", text.lower()))
    haystack = " ".join(re.findall(r"\w+", text.lower()))
    return any(" ".join(words[i : i + n]) in haystack for i in range(len(words) - n + 1))

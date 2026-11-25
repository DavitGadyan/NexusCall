"""Shared test constants for email_inbox_service (unique module name; test dirs are not packages)."""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from typing import Any

from nexus_core.celery_app import JobContext

EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
READ = {"emails:inbox:read"}
REVIEW = {"emails:inbox:read", "emails:drafts:review"}
APPROVE = {"emails:inbox:read", "emails:drafts:review", "emails:drafts:approve"}
MANAGE = {"emails:campaigns:manage", "emails:inbox:read"}


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@dataclass
class Job:
    name: str
    ctx: JobContext
    ids: dict[str, str]


def draft_result(body: dict[str, Any]) -> dict[str, Any]:
    """A schema-valid ai-agent drafting response (BUILD_CONVENTIONS §5)."""
    return {
        "run_id": str(uuid.uuid4()),
        "intent": "question",
        "priority": "normal",
        "sensitive": False,
        "summary": "The customer asks a question.",
        "draft_text": "Hello,\n\nThanks for your message. We are open Monday to Friday, 9am to 5pm.\n\nBest regards",
        "citations": [
            {"chunk_id": str(uuid.uuid4()), "source_id": str(uuid.uuid4()), "source_name": "Opening hours FAQ"}
        ],
        "quality": {"confidence": 0.82, "flags": []},
        "model": "claude-sonnet-5-5",
        "provider": "fake",
        "prompt_version": 3,
    }

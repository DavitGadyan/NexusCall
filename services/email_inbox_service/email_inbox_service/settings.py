from __future__ import annotations

from functools import lru_cache

from nexus_core.config import ServiceSettings

# Celery queues owned by this service (nexus_core.celery_app.QUEUES).
INBOUND_QUEUE = "emails.inbound_process"
DRAFT_QUEUE = "emails.ai_draft"
SEND_QUEUE = "emails.send_approved"
INBOUND_TASK = "emails.inbound_process"
DRAFT_TASK = "emails.ai_draft"
SEND_TASK = "emails.send_approved"


class Settings(ServiceSettings):
    service_name: str = "email-inbox-service"

    # ---- SES inbound (SNS notification -> raw MIME in the `raw-mail` bucket)
    # Real mode: only these SNS topics are accepted. Empty means "accept none" (fail closed).
    ses_inbound_topic_arns: tuple[str, ...] = ()
    # Fake mode (development/test only): the local simulator's topic.
    fake_ses_inbound_topic_arn: str = "arn:aws:sns:us-east-1:000000000000:nexus-inbound-dev"
    ses_inbound_bucket: str = "raw-mail"
    ses_inbound_key_prefix: str = "inbound/"
    sns_max_age_seconds: int = 3600

    # ---- MIME and attachment limits (build.md §4.2 step 6)
    max_raw_mime_bytes: int = 25 * 1024 * 1024
    max_attachment_bytes: int = 10 * 1024 * 1024
    max_attachments: int = 20
    max_text_chars: int = 200_000
    allowed_attachment_types: tuple[str, ...] = (
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/gif",
        "text/plain",
        "text/csv",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    # ---- ClamAV (clamd TCP INSTREAM)
    clamav_host: str | None = "clamav"
    clamav_port: int = 3310
    clamav_timeout_seconds: float = 30.0

    # ---- Drafting context bounds (OWASP LLM06)
    draft_context_messages: int = 6
    draft_context_chars_per_message: int = 8_000
    max_draft_chars: int = 20_000
    draft_generation_max_attempts: int = 5

    # ---- Sending
    send_max_attempts: int = 8
    attachment_url_ttl_seconds: int = 300

    # ---- Background jobs
    sweeper_interval_seconds: float = 60.0
    stale_seconds: int = 600
    requeue_after_seconds: int = 120
    retention_interval_seconds: float = 3600.0
    provider_event_retention_days: int = 30


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

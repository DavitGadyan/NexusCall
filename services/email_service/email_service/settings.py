from __future__ import annotations

from functools import lru_cache

from nexus_core.config import ServiceSettings

FAKE_SES_EVENTS_TOPIC = "arn:aws:sns:us-east-1:000000000000:nexus-ses-events"


class Settings(ServiceSettings):
    service_name: str = "email-service"
    # Callers of /internal/v1/* (send + message lookup).
    internal_send_clients: tuple[str, ...] = ("svc-email-inbox-service",)

    # Amazon SES (real mode). Events arrive through a configuration set -> SNS topic -> /webhooks/ses/events.
    ses_configuration_set: str | None = None
    ses_events_topic_arns: tuple[str, ...] = ()
    ses_timeout_seconds: float = 10.0
    sns_confirm_subscriptions: bool = True

    # Sending pipeline
    # Non-campaign sends (reply, transactional). A dedicated latency queue is an integration request; until it
    # exists they share the email worker queue (emails.send_approved belongs to email-inbox-service's task).
    transactional_queue: str = "emails.campaign_send"
    send_max_attempts: int = 5
    retry_base_seconds: float = 30.0
    retry_max_seconds: float = 1800.0
    kill_switch_defer_seconds: int = 300
    # Token buckets (Redis). Provider: SES default account rate is 14/s. Per sending domain, per stream.
    provider_rate_per_second: float = 14.0
    provider_burst: int = 14
    domain_rate_per_second: float = 5.0
    domain_burst: int = 10
    transactional_domain_rate_per_second: float = 5.0
    transactional_domain_burst: int = 20
    test_sends_per_hour: int = 20

    # Scheduler / sweepers (PeriodicJobs, Redis-locked)
    scheduler_interval_seconds: float = 5.0
    scheduler_batch_max: int = 200  # messages promoted to queued per campaign per tick
    snapshot_page_size: int = 500
    sweeper_interval_seconds: float = 60.0
    stuck_queued_seconds: int = 120
    stuck_sending_seconds: int = 300
    # When the provider cannot be asked whether an ambiguous send happened (real SES), wait this long for an
    # SES event before giving up as `failed` (never resent: no duplicates).
    ambiguous_give_up_seconds: int = 3600
    suppression_sync_interval_seconds: float = 30.0
    active_tenant_days: int = 14

    # Deliverability guardrails (THREAT_MODEL "email abuse"): auto-pause thresholds per campaign.
    bounce_rate_threshold: float = 0.05
    complaint_rate_threshold: float = 0.001
    threshold_min_sample: int = 50

    # Domain verification
    dns_timeout_seconds: float = 4.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

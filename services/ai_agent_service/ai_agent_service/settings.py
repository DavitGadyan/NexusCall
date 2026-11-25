from __future__ import annotations

from functools import lru_cache

from nexus_core.config import ServiceSettings

# Internal callers per endpoint (docs/BUILD_CONVENTIONS.md §5, ai-agent-service). One client id per service.
AGENT_READERS = (
    "svc-campaign-service",
    "svc-call-service",
    "svc-email-inbox-service",
    "svc-mcp-registry",
    "svc-voice-service",
)
RUNTIME_CONFIG_READERS = ("svc-voice-service", "svc-call-service")
KNOWLEDGE_SEARCHERS = ("svc-tool-execution", "svc-email-inbox-service")
DRAFTING_CALLERS = ("svc-email-inbox-service",)
POSTPROCESS_CALLERS = ("svc-voice-service",)

INGEST_QUEUE = "ai.knowledge_ingest"


class Settings(ServiceSettings):
    service_name: str = "ai-agent-service"
    # Uploads are up to 10 MB; allow multipart overhead. JSON bodies are bounded by their models.
    max_request_body_bytes: int = 11 * 1024 * 1024

    # Ingestion limits (OWASP LLM05 / LLM06)
    knowledge_max_upload_bytes: int = 10 * 1024 * 1024
    knowledge_max_extracted_chars: int = 1_000_000
    knowledge_max_pdf_pages: int = 500
    knowledge_chunk_chars: int = 1200
    knowledge_chunk_overlap: int = 150
    knowledge_max_chunks: int = 1000
    knowledge_analysis_batch_blocks: int = 40
    malware_scan: str = "builtin"  # builtin | off

    # Retrieval (OWASP LLM09). The embedding model must match the gateway's configured embedding model.
    embedding_model: str = "voyage-3"
    retrieval_min_similarity: float = 0.1
    retrieval_max_per_document: int = 2
    search_rate_per_minute_tenant: int = 600
    search_rate_per_minute_user: int = 30
    retrieval_failure_window_seconds: int = 600
    retrieval_failure_alert_threshold: int = 20

    # Gateway calls carry the purpose deadline plus a margin.
    gateway_timeout_seconds: float = 130.0

    # Background jobs left queued longer than this are re-enqueued by the sweeper.
    job_stale_seconds: int = 600
    job_max_attempts: int = 20
    sweeper_interval_seconds: float = 60.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

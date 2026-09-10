"""
services/document-forgery-engine/config.py
==========================================
Centralised configuration for the Document Forgery Engine (task 15.1).

All runtime configuration is read from environment variables (see
``.env.example``) using ``pydantic-settings``. This keeps model paths and
tuning thresholds out of source code and lets the service degrade gracefully in
local development / the CI sandbox, where the multi-gigabyte LayoutLMv3 +
``transformers`` + ``torch`` stack is not installed.

Two engine thresholds are configured here and are exercised by boundary tests:

  * ``ocr_confidence_threshold`` — LayoutLMv3 per-field confidence floor
    (default 0.85). Any extracted field whose confidence is *strictly below*
    this value is flagged ``EXTRACTION_LOW_CONFIDENCE`` (design step 1,
    Requirement 7.1). Boundary: exactly 0.85 -> NO flag; 0.84 -> flag.

  * ``earnings_deviation_threshold`` — the fractional tolerance between the
    declared earnings and ``hours * hourly_rate`` (default 0.02, i.e. 2%). A
    deviation *strictly greater* than this fraction produces a CRITICAL
    Indicator (design step 2a, Requirement 7.2). Boundary: exactly 2% -> NO
    flag; 2.01% -> flag.

Requirements: 7.1, 7.2
"""

from __future__ import annotations

from functools import lru_cache

try:  # pydantic-settings is the canonical settings library for pydantic v2
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAS_PYDANTIC_SETTINGS = True
except ImportError:  # pragma: no cover - fallback for minimal environments
    _HAS_PYDANTIC_SETTINGS = False


if _HAS_PYDANTIC_SETTINGS:

    class Settings(BaseSettings):
        """Environment-driven configuration for the Document Forgery Engine."""

        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

        # ── Deployment ──────────────────────────────────────────────
        app_env: str = "development"
        service_name: str = "document-forgery-engine"
        document_forgery_engine_port: int = 8015

        # ── LayoutLMv3 OCR model ────────────────────────────────────
        # Hugging Face model id / local path for the fine-tuned LayoutLMv3
        # token-classification checkpoint used to extract ROE fields. In
        # production this points at a mounted model artefact; in dev/test the
        # model is never loaded and the StubOCRExtractor is used instead.
        layoutlmv3_model_path: str = "microsoft/layoutlmv3-base"
        # When True (default in dev/test), force the deterministic stub extractor
        # regardless of whether transformers/torch happen to be importable, so
        # unit tests never trigger a multi-GB model download.
        use_stub_ocr: bool = True

        # ── Engine thresholds ───────────────────────────────────────
        # Per-field confidence floor for LayoutLMv3 extraction (Requirement 7.1).
        ocr_confidence_threshold: float = 0.85
        # Fractional tolerance for hours*hourly_rate vs earnings (Requirement 7.2).
        # 0.02 == 2%. Deviation strictly greater than this fraction -> CRITICAL.
        earnings_deviation_threshold: str = "0.02"

        # ── Kafka (Indicator publication) ──────────────────────────
        kafka_bootstrap_servers: str = "localhost:9092"
        indicators_topic: str = "indicators.document-forgery"
        # When True (default in dev/test), the engine does not attempt to build
        # a live aiokafka producer; it simply returns Indicators in the HTTP
        # response. Set False in production to publish to Kafka.
        enable_kafka: bool = False

        # ── Internal service auth (Req 19.7) ────────────────────────
        # Service Bearer token presented on the internal CRA Adapter (payroll)
        # + LLM Service calls. Every EI-ADS endpoint validates a GCKey Bearer
        # (Req 19.7), so the engine authenticates with its own service
        # credential — mirroring the History/Regional/Geographic/Fraud-Ring
        # engines' SERVICE_TOKEN pattern. Empty in unit tests (no header sent).
        service_token: str = ""

        # ── External dependencies (task 15.3) ──────────────────────
        # CRA Adapter base URL for payroll cross-reference (Requirement 7.7).
        cra_adapter_base_url: str = "http://cra-adapter:8011"
        cra_timeout_seconds: float = 5.0
        # LLM Service base URL for async termination-narrative style analysis
        # (Requirements 7.8, 7.9). Port 8005 matches the llm-service Containerfile.
        llm_service_base_url: str = "http://llm-service:8005"
        llm_timeout_seconds: float = 5.0

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

        @property
        def force_stub_ocr(self) -> bool:
            """True when the deterministic stub extractor must be used."""
            return self.use_stub_ocr or self.is_development

else:  # pragma: no cover - executed only when pydantic-settings is absent

    import os

    class Settings:  # type: ignore[no-redef]
        """Minimal environment-backed settings fallback (no pydantic-settings)."""

        def __init__(self) -> None:
            self.app_env = os.environ.get("APP_ENV", "development")
            self.service_name = os.environ.get(
                "SERVICE_NAME", "document-forgery-engine"
            )
            self.document_forgery_engine_port = int(
                os.environ.get("DOCUMENT_FORGERY_ENGINE_PORT", "8015")
            )

            self.layoutlmv3_model_path = os.environ.get(
                "LAYOUTLMV3_MODEL_PATH", "microsoft/layoutlmv3-base"
            )
            self.use_stub_ocr = os.environ.get("USE_STUB_OCR", "true").lower() in {
                "1",
                "true",
                "yes",
            }

            self.ocr_confidence_threshold = float(
                os.environ.get("OCR_CONFIDENCE_THRESHOLD", "0.85")
            )
            self.earnings_deviation_threshold = os.environ.get(
                "EARNINGS_DEVIATION_THRESHOLD", "0.02"
            )

            self.kafka_bootstrap_servers = os.environ.get(
                "KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"
            )
            self.indicators_topic = os.environ.get(
                "INDICATORS_TOPIC", "indicators.document-forgery"
            )
            self.enable_kafka = os.environ.get("ENABLE_KAFKA", "false").lower() in {
                "1",
                "true",
                "yes",
            }

            # Internal service Bearer token (Req 19.7); empty in unit tests.
            self.service_token = os.environ.get("SERVICE_TOKEN", "")

            self.cra_adapter_base_url = os.environ.get(
                "CRA_ADAPTER_BASE_URL", "http://cra-adapter:8011"
            )
            self.cra_timeout_seconds = float(
                os.environ.get("CRA_TIMEOUT_SECONDS", "5.0")
            )
            self.llm_service_base_url = os.environ.get(
                "LLM_SERVICE_BASE_URL", "http://llm-service:8005"
            )
            self.llm_timeout_seconds = float(
                os.environ.get("LLM_TIMEOUT_SECONDS", "5.0")
            )

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

        @property
        def force_stub_ocr(self) -> bool:
            return self.use_stub_ocr or self.is_development


@lru_cache
def get_settings() -> "Settings":
    """Return a cached ``Settings`` instance."""
    return Settings()


__all__ = ["Settings", "get_settings"]

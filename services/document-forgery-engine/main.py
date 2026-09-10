"""
services/document-forgery-engine/main.py
========================================
FastAPI application for the Document Forgery Engine (task 15.1).

Endpoints
---------
``POST /engine/analyze``
    Accepts a claim/document payload and runs the task-15.1 slice of the
    Document Forgery Engine pipeline (design "5. Document Forgery Engine",
    steps 1-2):

      1. OCR extraction of the 11 mandatory ROE fields with per-field
         confidence (StubOCRExtractor in the sandbox; LayoutLMv3 in
         production).
      2. ``EXTRACTION_LOW_CONFIDENCE`` flagging for any field below the 0.85
         confidence floor (Requirement 7.1).
      3. ``hours * hourly_rate`` vs declared earnings consistency check
         (CRITICAL when deviation > 2%, Requirement 7.2).
      4. ``last_day_paid < first_day_worked`` date-order check (CRITICAL,
         Requirement 7.2).

    Returns the produced Indicators as JSON. The remaining pipeline steps (PDF
    metadata forensics, font fingerprinting, signature verification, payroll
    cross-reference, LLM style analysis) are implemented by later sub-tasks
    (15.2, 15.3) and are intentionally out of scope here.

``GET /healthz``
    Liveness/readiness probe reporting whether the deterministic stub extractor
    is in use.

Requirements: 7.1, 7.2
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from datetime import date, datetime
from typing import Any, Optional

from fastapi import Depends, FastAPI, status

from shared.auth import Role, require_role
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:  # sibling modules imported by bare name in the service directory
    from config import get_settings  # type: ignore
    from ocr import get_extractor, low_confidence_indicators  # type: ignore
    from consistency import (  # type: ignore
        check_date_order,
        check_hours_rate_earnings,
    )
    from metadata_forensics import (  # type: ignore
        check_pdf_creation_vs_issue_date,
        extract_pdf_creation_timestamp,
    )
    from font_fingerprint import (  # type: ignore
        FontProfile,
        check_font_profile,
        extract_font_profile,
    )
    from signature_verification import (  # type: ignore
        check_digital_signature,
        extract_signature_certificate_hash,
    )
    from payroll_cross_reference import (  # type: ignore
        CRAPayrollClient,
        check_payroll_cross_reference,
    )
    from style_analysis import StyleAnalysisChecker  # type: ignore
    from kafka_publisher import (  # type: ignore
        AIOKafkaProducerAdapter,
        IndicatorPublisher,
        ANALYSIS_BUDGET_MS,
        make_processing_timeout_indicator,
    )
except ImportError:  # pragma: no cover - when imported as a package
    from .config import get_settings  # type: ignore
    from .ocr import get_extractor, low_confidence_indicators  # type: ignore
    from .consistency import (  # type: ignore
        check_date_order,
        check_hours_rate_earnings,
    )
    from .metadata_forensics import (  # type: ignore
        check_pdf_creation_vs_issue_date,
        extract_pdf_creation_timestamp,
    )
    from .font_fingerprint import (  # type: ignore
        FontProfile,
        check_font_profile,
        extract_font_profile,
    )
    from .signature_verification import (  # type: ignore
        check_digital_signature,
        extract_signature_certificate_hash,
    )
    from .payroll_cross_reference import (  # type: ignore
        CRAPayrollClient,
        check_payroll_cross_reference,
    )
    from .style_analysis import StyleAnalysisChecker  # type: ignore
    from .kafka_publisher import (  # type: ignore
        AIOKafkaProducerAdapter,
        IndicatorPublisher,
        ANALYSIS_BUDGET_MS,
        make_processing_timeout_indicator,
    )

logger = logging.getLogger("document-forgery-engine.main")


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class AnalyzeRequest(BaseModel):
    """Payload for ``POST /engine/analyze``.

    ``document`` is the OCR extraction input. In the sandbox it may carry
    pre-known ``fields`` and per-field ``confidences`` so callers can drive the
    stub deterministically; in production it carries the raw ROE document.
    """

    claim_id: Optional[str] = Field(default=None)
    document: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Lifespan handler
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Wire configuration, the OCR extractor, and outbound clients / publisher."""
    settings = get_settings()
    app.state.settings = settings
    app.state.extractor = get_extractor(settings)

    # Kafka publisher (disabled by default in dev/test — no live broker needed).
    producer = None
    if getattr(settings, "enable_kafka", False):
        producer = AIOKafkaProducerAdapter(
            bootstrap_servers=getattr(settings, "kafka_bootstrap_servers", "localhost:9092")
        )
    app.state.publisher = IndicatorPublisher(
        producer, topic=getattr(settings, "indicators_topic", "indicators.document-forgery")
    )

    # CRA Adapter payroll client (Requirement 7.7). Presents the engine's
    # internal GCKey service Bearer (Req 19.7); empty => no header (unit tests).
    app.state.payroll_client = CRAPayrollClient(
        base_url=getattr(settings, "cra_adapter_base_url", ""),
        timeout_seconds=float(getattr(settings, "cra_timeout_seconds", 5.0)),
        bearer_token=getattr(settings, "service_token", "") or None,
    )
    yield
    # Best-effort producer shutdown.
    if producer is not None:  # pragma: no cover - only with a live broker
        await producer.stop()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="EI-ADS Document Forgery Engine",
    version="0.1.0",
    lifespan=lifespan,
)

# ── OpenTelemetry distributed tracing (shared.telemetry, task 32.1 / Req 21.5) ──
# Single, self-contained hook: all tracer setup and span helpers live in
# shared/telemetry.py. No-op when the OpenTelemetry packages are absent.
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="document-forgery-engine")
# ── end OpenTelemetry tracing ──

# --- Prometheus metrics + /metrics endpoint (task 31.1) ---
from shared.metrics import instrument_app  # noqa: E402

instrument_app(app, kind="engine")
# --- end metrics wiring ---


def _get_extractor(app_: FastAPI):
    extractor = getattr(app_.state, "extractor", None)
    if extractor is None:
        settings = getattr(app_.state, "settings", None) or get_settings()
        extractor = get_extractor(settings)
        app_.state.extractor = extractor
    return extractor


@app.post("/engine/analyze")
async def analyze(
    request: AnalyzeRequest,
    _user: dict[str, Any] = Depends(require_role(Role.INVESTIGATOR)),
) -> JSONResponse:
    """Run the full task-15.1/15.2/15.3 Document Forgery Engine pipeline.

    Steps 1-6 (OCR + consistency + metadata forensics + font fingerprint +
    signature verification + payroll cross-reference) run within the 700 ms
    core budget and their Indicators are published to Kafka. The async LLM
    style analysis (step 7) runs off the critical path. If the core analysis
    exceeds 700 ms a PROCESSING_TIMEOUT Indicator is emitted (Requirement 7.11).
    """
    settings = getattr(app.state, "settings", None) or get_settings()
    extractor = _get_extractor(app)

    core_start = time.monotonic()

    conf_threshold = float(getattr(settings, "ocr_confidence_threshold", 0.85))
    dev_threshold = _as_decimal(
        getattr(settings, "earnings_deviation_threshold", "0.02"), Decimal("0.02")
    )

    indicators = []

    # 1 + 2. OCR extraction of the 11 mandatory fields and low-confidence flags.
    extracted = extractor.extract(request.document)
    indicators.extend(
        low_confidence_indicators(
            extracted, claim_id=request.claim_id, threshold=conf_threshold
        )
    )

    # 3. hours * hourly_rate vs earnings. Prefer explicit request values, then
    #    fall back to the extracted OCR fields.
    hours = _resolve_decimal(request.document, extracted, "total_insurable_hours", "hours")
    hourly_rate = _resolve_decimal(request.document, extracted, "hourly_rate", "hourly_rate")
    earnings = _resolve_decimal(
        request.document, extracted, "total_insurable_earnings", "earnings"
    )
    if hours is not None and hourly_rate is not None and earnings is not None:
        indicators.extend(
            check_hours_rate_earnings(
                hours,
                hourly_rate,
                earnings,
                claim_id=request.claim_id,
                threshold=dev_threshold,
            )
        )

    # 4. last_day_paid < first_day_worked date-order check.
    last_day_paid = _resolve_date(request.document, extracted, "last_day_paid")
    first_day_worked = _resolve_date(request.document, extracted, "first_day_worked")
    if last_day_paid is not None and first_day_worked is not None:
        indicators.extend(
            check_date_order(
                last_day_paid, first_day_worked, claim_id=request.claim_id
            )
        )

    # 5. PDF metadata forensics (design step 3, Requirement 7.3). The creation
    #    timestamp is taken from the request when supplied (already parsed
    #    upstream), otherwise extracted from the PDF bytes/path via pypdf.
    creation_ts = _resolve_datetime(request.document, "pdf_creation_timestamp")
    if creation_ts is None:
        creation_ts = extract_pdf_creation_timestamp(_resolve_pdf(request.document))
    issue_date = _resolve_datetime(request.document, "issue_date")
    if creation_ts is not None and issue_date is not None:
        indicators.extend(
            check_pdf_creation_vs_issue_date(
                creation_ts, issue_date, claim_id=request.claim_id
            )
        )

    # 6. Font fingerprinting (design step 4, Requirement 7.4). Compare the
    #    submitted ROE's dominant font profile to the employer's registry
    #    profile; skipped when the employer has < 3 prior authenticated ROEs.
    registry = request.document.get("document_registry") or {}
    prior_count = int(registry.get("prior_authenticated_roe_count", 0) or 0)
    expected_profile = _resolve_font_profile(registry.get("expected_font_profile"))
    observed_profile = _resolve_font_profile(request.document.get("font_profile"))
    if observed_profile is None:
        observed_profile = extract_font_profile(_resolve_pdf(request.document))
    indicators.extend(
        check_font_profile(
            observed_profile,
            expected_profile,
            prior_authenticated_roe_count=prior_count,
            claim_id=request.claim_id,
        )
    )

    # 7. Digital signature verification (design step 5, Requirements 7.5, 7.6).
    #    Extract the signer certificate hash from the embedded PKCS#7 / CAdES
    #    signature (cryptography) and compare it to the employer's registered
    #    X.509 certificate hash from the signature registry.
    signature_registry = request.document.get("signature_registry") or {}
    registered_cert_hash = (
        signature_registry.get("registered_cert_hash")
        or request.document.get("registered_cert_hash")
    )
    extraction = extract_signature_certificate_hash(request.document)
    indicators.extend(
        check_digital_signature(
            extraction, registered_cert_hash, claim_id=request.claim_id
        )
    )

    # 8. Payroll cross-reference (design step 6, Requirement 7.7). Query the CRA
    #    Adapter for the employee SIN in the employer's payroll for the
    #    employment period; a confirmed absence is a CRITICAL Indicator.
    indicators.extend(
        await _run_payroll_cross_reference(request, extracted)
    )

    # 700 ms core budget accounting (Requirements 7.10, 7.11). The async LLM
    # style check runs off this critical path.
    core_elapsed_ms = (time.monotonic() - core_start) * 1000.0
    if core_elapsed_ms > ANALYSIS_BUDGET_MS:
        indicators.append(
            make_processing_timeout_indicator(
                core_elapsed_ms, claim_id=request.claim_id
            )
        )

    # 9. LLM termination-narrative style analysis (design step 7, async,
    #    Requirements 7.8, 7.9). Off the 700 ms critical path; skipped when the
    #    employer has < 5 prior authenticated ROEs.
    indicators.extend(await _run_style_analysis(request))

    # Publish all Indicators to indicators.document-forgery (design step 8).
    publisher = getattr(app.state, "publisher", None)
    published_count = 0
    if publisher is not None:
        publish_result = await publisher.publish(request.claim_id, indicators)
        published_count = publish_result.published_count

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "claim_id": request.claim_id,
            "extraction_source": extracted.source,
            "indicator_count": len(indicators),
            "core_elapsed_ms": round(core_elapsed_ms, 4),
            "published_count": published_count,
            "indicators": [i.to_dict() for i in indicators],
        },
    )


async def _run_payroll_cross_reference(
    request: "AnalyzeRequest", extracted: Any
) -> list:
    """Query the CRA Adapter payroll endpoint and flag a confirmed absent SIN.

    The payroll client is taken from ``app.state`` (injectable in tests). The
    SIN hash and employment period are resolved from the request document,
    falling back to the OCR-extracted fields. When the required inputs are
    missing, or no client is configured, the check is skipped.
    """
    document = request.document
    client = getattr(app.state, "payroll_client", None)
    if client is None:
        return []

    sin_hash = document.get("sin_hash") or document.get("sin")
    if sin_hash is None:
        sin_hash = extracted.values.get("sin")
    employer_bn = document.get("employer_bn")
    if employer_bn is None:
        employer_bn = extracted.values.get("employer_bn")
    employment_start = (
        document.get("employment_start")
        or document.get("first_day_worked")
        or extracted.values.get("first_day_worked")
    )
    employment_end = (
        document.get("employment_end")
        or document.get("last_day_paid")
        or extracted.values.get("last_day_paid")
    )
    if not (sin_hash and employer_bn and employment_start and employment_end):
        return []

    lookup = await client.check_sin_in_payroll(
        str(sin_hash), str(employer_bn), str(employment_start), str(employment_end)
    )
    return check_payroll_cross_reference(
        lookup,
        sin_hash=str(sin_hash),
        employer_bn=str(employer_bn),
        claim_id=request.claim_id,
    )


async def _run_style_analysis(request: "AnalyzeRequest") -> list:
    """Run the async LLM termination-narrative style check when configured.

    The :class:`StyleAnalysisChecker` is taken from ``app.state.style_checker``
    (injected in tests / wired to the LLM Service in production). When no
    checker is configured, or the required narrative inputs are missing, the
    check is skipped. The corpus size gate (< 5 -> skip) is enforced inside the
    checker (Requirement 7.9).
    """
    checker = getattr(app.state, "style_checker", None)
    if checker is None:
        return []

    document = request.document
    registry = document.get("document_registry") or {}
    narrative = (
        document.get("termination_narrative")
        or document.get("reason_for_separation")
        or ""
    )
    prior_narratives = (
        registry.get("prior_roe_narratives")
        or document.get("prior_roe_narratives")
        or []
    )
    prior_count = registry.get("prior_authenticated_roe_count")
    if not narrative:
        return []

    return await checker.check(
        str(narrative),
        list(prior_narratives),
        prior_authenticated_roe_count=(
            int(prior_count) if prior_count is not None else None
        ),
        claim_id=request.claim_id,
    )


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Report basic readiness for the Document Forgery Engine."""
    extractor = getattr(app.state, "extractor", None)
    source = getattr(extractor, "source", "unknown") if extractor else "unknown"
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "ok", "extraction_source": source},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_decimal(value: Any, default: Decimal) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _resolve_decimal(
    document: dict[str, Any],
    extracted: Any,
    field_name: str,
    request_key: str,
) -> Optional[Decimal]:
    """Resolve a numeric field from the top-level request, then the OCR fields."""
    if request_key in document and document[request_key] is not None:
        try:
            return Decimal(str(document[request_key]))
        except (InvalidOperation, ValueError, TypeError):
            return None
    return extracted.as_decimal(field_name)


def _resolve_date(
    document: dict[str, Any], extracted: Any, field_name: str
) -> Optional[date]:
    """Resolve a date field from the top-level request, then the OCR fields."""
    raw = document.get(field_name)
    if raw is None:
        raw = extracted.values.get(field_name)
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return None


def _resolve_datetime(document: dict[str, Any], field_name: str) -> Optional[datetime]:
    """Resolve a timestamp field (ISO-8601 date or datetime) from the request.

    Accepts either a full datetime (``2024-01-15T12:00:00+00:00``) or a bare
    date (``2024-01-15``, interpreted as midnight). Returns ``None`` when
    absent or unparseable.
    """
    raw = document.get(field_name)
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day)
    text = str(raw)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    try:
        d = date.fromisoformat(text)
        return datetime(d.year, d.month, d.day)
    except ValueError:
        return None


def _resolve_pdf(document: dict[str, Any]) -> Any:
    """Resolve the PDF payload (path/bytes) for pypdf / pdfminer extraction."""
    for key in ("pdf_path", "pdf_bytes", "pdf"):
        value = document.get(key)
        if value:
            return value
    return b""


def _resolve_font_profile(value: Any) -> Optional["FontProfile"]:
    """Coerce a font-profile dict into a :class:`FontProfile` (or pass through)."""
    if value is None:
        return None
    if isinstance(value, FontProfile):
        return value
    if isinstance(value, dict):
        family = value.get("font_family")
        size = value.get("point_size")
        if family is None or size is None:
            return None
        try:
            return FontProfile(font_family=str(family), point_size=float(size))
        except (TypeError, ValueError):
            return None
    return None


__all__ = ["app", "lifespan", "analyze", "healthz", "AnalyzeRequest"]

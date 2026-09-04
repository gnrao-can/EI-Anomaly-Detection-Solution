"""
shared/telemetry.py
====================
Single source of truth for EI-ADS distributed tracing (OpenTelemetry).

Every EI-ADS microservice imports its tracing setup and span helpers from this
module so there is exactly one place that:

  * builds and configures the OpenTelemetry ``TracerProvider`` and exports spans
    to the OpenTelemetry Collector over OTLP (configured in ``podman-compose.yml``
    and ``k8s/otel-collector.yaml``);
  * instruments a FastAPI app (plus asyncpg and aiohttp client) with a single,
    minimal call — :func:`instrument_tracing` — so each service's ``main.py``
    needs only ONE contiguous, clearly-commented block near app creation;
  * creates the design's trace structure: a root ``claim.received`` span in the
    Pipeline Orchestrator and child spans for each engine call, adapter call,
    SHAP explanation, and LLM call (see :func:`engine_span`, :func:`adapter_span`,
    :func:`shap_span`, :func:`llm_span`);
  * propagates trace context across Kafka via message headers
    (:func:`inject_trace_context` / :func:`extract_trace_context`); and
  * ENFORCES the Protected B rule that NO raw PII (SIN, name, address, ...) ever
    appears in a span attribute — only ``sin_hash`` and non-PII identifiers are
    permitted (:func:`set_span_attributes`, :func:`safe_span_attributes`).

Guarded imports
---------------
The ``opentelemetry-*`` packages are optional at import time. In the sandbox /
unit-test environment they are typically absent, so every OpenTelemetry import is
guarded behind a try/except and the module degrades to inert no-ops. Services
therefore import and start WITHOUT the OpenTelemetry packages installed; when the
packages ARE present (production images, per ``scripts/dependencies``), tracing
is fully active. The PII-guard helpers work identically in both modes, so the
"no raw PII in spans" invariant is testable without the OTel stack installed.

Requirements: 21.5
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, MutableMapping, Optional

logger = logging.getLogger("shared.telemetry")

# ---------------------------------------------------------------------------
# Guarded OpenTelemetry imports
# ---------------------------------------------------------------------------
# Everything OTel is optional. When absent, ``_OTEL_AVAILABLE`` is False and all
# public helpers become safe no-ops so services import and run in the sandbox.

try:  # pragma: no cover - exercised only when the OTel SDK is installed
    from opentelemetry import trace as _otel_trace
    from opentelemetry.context import Context as _OtelContext
    from opentelemetry.propagate import extract as _otel_extract
    from opentelemetry.propagate import inject as _otel_inject
    from opentelemetry.sdk.resources import Resource as _OtelResource
    from opentelemetry.sdk.trace import TracerProvider as _OtelTracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor as _OtelBatchSpanProcessor

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - the sandbox path
    _otel_trace = None  # type: ignore[assignment]
    _OtelContext = None  # type: ignore[assignment,misc]
    _otel_extract = None  # type: ignore[assignment]
    _otel_inject = None  # type: ignore[assignment]
    _OtelResource = None  # type: ignore[assignment,misc]
    _OtelTracerProvider = None  # type: ignore[assignment,misc]
    _OtelBatchSpanProcessor = None  # type: ignore[assignment,misc]
    _OTEL_AVAILABLE = False


# ---------------------------------------------------------------------------
# PII guard
# ---------------------------------------------------------------------------
# Protected B: raw PII must NEVER be attached to a span. Only ``sin_hash`` and
# non-PII identifiers/metadata are permitted. We reject attribute keys that name
# a raw-PII field and (defensively) values that structurally look like a raw SIN.

#: Attribute keys that are forbidden on any span because they denote raw PII.
#: ``sin`` is forbidden; ``sin_hash`` is explicitly allowed (it is not raw PII).
FORBIDDEN_PII_KEYS: frozenset[str] = frozenset(
    {
        "sin",
        "social_insurance_number",
        "social_insurance_no",
        "name",
        "first_name",
        "last_name",
        "middle_name",
        "full_name",
        "given_name",
        "surname",
        "address",
        "street_address",
        "home_address",
        "mailing_address",
        "postal_code",
        "date_of_birth",
        "dob",
        "birth_date",
        "phone",
        "phone_number",
        "email",
        "email_address",
    }
)


class PIIViolationError(ValueError):
    """Raised when a span attribute would leak raw PII (SIN, name, address)."""


def _looks_like_raw_sin(value: Any) -> bool:
    """Return True when ``value`` structurally looks like a raw 9-digit SIN.

    A raw Canadian SIN is nine digits (optionally spaced/hyphenated in groups of
    three). ``sin_hash`` values are 64 hex chars, so they never match. This is a
    defensive backstop; the primary guard is the forbidden-key check.
    """
    if not isinstance(value, str):
        return False
    digits = value.replace(" ", "").replace("-", "")
    return len(digits) == 9 and digits.isdigit()


def _normalise_key(key: str) -> str:
    """Normalise an attribute key for forbidden-key comparison.

    Strips a namespacing prefix (e.g. ``claim.sin`` -> ``sin``) and lowercases,
    so both bare and dotted keys are checked against the forbidden set.
    """
    tail = key.rsplit(".", 1)[-1]
    return tail.strip().lower()


def assert_no_pii(key: str, value: Any) -> None:
    """Raise :class:`PIIViolationError` if ``(key, value)`` would leak raw PII.

    ``sin_hash`` is always permitted. Any key naming a raw-PII field, or any
    value that structurally looks like a raw SIN, is rejected.
    """
    norm = _normalise_key(key)
    if norm == "sin_hash":
        return
    if norm in FORBIDDEN_PII_KEYS:
        raise PIIViolationError(
            f"span attribute {key!r} names raw PII; use sin_hash and non-PII "
            f"identifiers only"
        )
    if _looks_like_raw_sin(value):
        raise PIIViolationError(
            f"span attribute {key!r} has a value that looks like a raw SIN; "
            f"attach sin_hash instead"
        )


def safe_span_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a mapping of span attributes against the PII guard.

    Returns a plain dict of the same attributes when all pass. Raises
    :class:`PIIViolationError` on the first offending key/value. ``None`` values
    are dropped (OpenTelemetry rejects ``None`` attribute values).
    """
    safe: dict[str, Any] = {}
    for key, value in attributes.items():
        assert_no_pii(key, value)
        if value is not None:
            safe[key] = value
    return safe


# ---------------------------------------------------------------------------
# Tracer provider setup
# ---------------------------------------------------------------------------

#: Process-wide flag so the provider is configured at most once.
_provider_configured = False


def _otlp_endpoint() -> str:
    """Return the OTLP exporter endpoint for the OpenTelemetry Collector.

    Honours the standard ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var; defaults to the
    Collector's in-cluster / compose service address.
    """
    return os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317"
    )


def configure_tracing(service_name: str) -> bool:
    """Configure the global OpenTelemetry ``TracerProvider`` once per process.

    Builds a ``TracerProvider`` tagged with ``service.name`` and attaches a
    ``BatchSpanProcessor`` that exports to the OpenTelemetry Collector over OTLP
    (gRPC). Safe to call multiple times: it configures the provider at most once.

    Returns ``True`` when tracing is active, ``False`` when the OpenTelemetry SDK
    is unavailable (sandbox) and tracing degrades to a no-op.
    """
    global _provider_configured

    if not _OTEL_AVAILABLE:
        logger.debug(
            "opentelemetry SDK not installed; tracing disabled for %s",
            service_name,
        )
        return False

    if _provider_configured:
        return True

    try:  # pragma: no cover - requires the OTel SDK + exporter to be installed
        resource = _OtelResource.create({"service.name": service_name})
        provider = _OtelTracerProvider(resource=resource)

        # OTLP exporter is a separate optional package; guard it independently so
        # a provider can still be set even if only the exporter is missing.
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )

            exporter = OTLPSpanExporter(endpoint=_otlp_endpoint(), insecure=True)
            provider.add_span_processor(_OtelBatchSpanProcessor(exporter))
        except ImportError:
            logger.warning(
                "OTLP gRPC exporter not installed; spans will not be exported "
                "for %s",
                service_name,
            )

        _otel_trace.set_tracer_provider(provider)
        _provider_configured = True
        logger.info("tracing configured for service %s", service_name)
        return True
    except Exception as exc:  # noqa: BLE001 - never let tracing setup crash a service
        logger.warning("failed to configure tracing for %s: %s", service_name, exc)
        return False


def get_tracer(name: str = "ei-ads"):
    """Return an OpenTelemetry tracer, or ``None`` when the SDK is unavailable."""
    if not _OTEL_AVAILABLE:
        return None
    return _otel_trace.get_tracer(name)


def instrument_tracing(app: Any = None, *, service_name: str) -> bool:
    """Attach OpenTelemetry tracing to a service — the single per-service hook.

    Each service's ``main.py`` calls this exactly once near app creation::

        # ── OpenTelemetry tracing (shared.telemetry, task 32.1) ──
        from shared.telemetry import instrument_tracing
        instrument_tracing(app, service_name="cbsa-adapter")

    It configures the global tracer provider (once) and best-effort instruments
    the FastAPI app plus the asyncpg and aiohttp-client integrations when those
    instrumentation packages are installed. Every step is guarded so a missing
    package (the sandbox default) leaves the service fully importable and the
    call becomes a no-op.

    ``app`` may be ``None`` (e.g. the LLM service builds ``app = None`` in a bare
    sandbox); in that case only the provider is configured.

    Returns ``True`` when tracing was activated, ``False`` otherwise.
    """
    active = configure_tracing(service_name)
    if not active:
        return False

    if app is not None:  # pragma: no cover - requires FastAPI instrumentation pkg
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(app)
        except Exception as exc:  # noqa: BLE001
            logger.debug("FastAPI instrumentation unavailable: %s", exc)

    # asyncpg and aiohttp-client instrumentation are global (not per-app).
    try:  # pragma: no cover - requires the asyncpg instrumentation pkg
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor

        AsyncPGInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001
        logger.debug("asyncpg instrumentation unavailable: %s", exc)

    try:  # pragma: no cover - requires the aiohttp-client instrumentation pkg
        from opentelemetry.instrumentation.aiohttp_client import (
            AioHttpClientInstrumentor,
        )

        AioHttpClientInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001
        logger.debug("aiohttp-client instrumentation unavailable: %s", exc)

    return True


# ---------------------------------------------------------------------------
# Span helpers
# ---------------------------------------------------------------------------


def set_span_attributes(span: Any, attributes: Mapping[str, Any]) -> None:
    """Attach PII-checked attributes to a span (no-op when span is ``None``).

    Every attribute passes through :func:`safe_span_attributes` first, so a raw
    SIN / name / address can never be written to a span.
    """
    safe = safe_span_attributes(attributes)
    if span is None:
        return
    setter = getattr(span, "set_attribute", None)
    if setter is None:
        return
    for key, value in safe.items():
        setter(key, value)


@contextmanager
def start_span(
    name: str,
    *,
    attributes: Optional[Mapping[str, Any]] = None,
    context: Any = None,
) -> Iterator[Any]:
    """Start a span as a context manager, yielding the span (or ``None``).

    Attributes are PII-checked BEFORE the span is created, so a PII violation is
    raised regardless of whether the OpenTelemetry SDK is installed — the guard
    is always enforced. When the SDK is unavailable the span object is ``None``
    and the block still runs.
    """
    safe = safe_span_attributes(attributes) if attributes else {}

    tracer = get_tracer()
    if tracer is None:
        yield None
        return

    # pragma: no cover below — only runs with the OTel SDK installed.
    span_cm = tracer.start_as_current_span(name, context=context)  # type: ignore[call-arg]
    with span_cm as span:  # pragma: no cover
        for key, value in safe.items():
            span.set_attribute(key, value)
        yield span


def root_claim_span(
    claim_id: str,
    sin_hash: str,
    *,
    attributes: Optional[Mapping[str, Any]] = None,
):
    """Start the design's root ``claim.received`` span (Pipeline Orchestrator).

    Carries ``claim_id`` and ``sin_hash`` (never a raw SIN) plus any extra non-PII
    attributes. Use as::

        with root_claim_span(claim_id, sin_hash) as span:
            inject_trace_context(kafka_headers)  # propagate to engines
            ...
    """
    attrs: dict[str, Any] = {"claim_id": claim_id, "sin_hash": sin_hash}
    if attributes:
        attrs.update(attributes)
    return start_span("claim.received", attributes=attrs)


def engine_span(engine_name: str, claim_id: str, **attributes: Any):
    """Start a child span for a detection-engine call: ``engine.<engine_name>``."""
    attrs: dict[str, Any] = {"claim_id": claim_id, "engine": engine_name}
    attrs.update(attributes)
    return start_span(f"engine.{engine_name}", attributes=attrs)


def adapter_span(adapter_name: str, operation: str, **attributes: Any):
    """Start a child span for an external-data adapter call: ``adapter.<name>.<op>``."""
    attrs: dict[str, Any] = {"adapter": adapter_name, "operation": operation}
    attrs.update(attributes)
    return start_span(f"adapter.{adapter_name}.{operation}", attributes=attrs)


def shap_span(claim_id: str, **attributes: Any):
    """Start a child span for the SHAP explanation step: ``shap.explain``."""
    attrs: dict[str, Any] = {"claim_id": claim_id}
    attrs.update(attributes)
    return start_span("shap.explain", attributes=attrs)


def llm_span(operation: str, **attributes: Any):
    """Start a child span for an LLM call: ``llm.<operation>`` (e.g. ``explain_bilingual``)."""
    attrs: dict[str, Any] = {"operation": operation}
    attrs.update(attributes)
    return start_span(f"llm.{operation}", attributes=attrs)


# ---------------------------------------------------------------------------
# Kafka trace-context propagation
# ---------------------------------------------------------------------------
# Kafka headers are a list of (key: str, value: bytes) tuples in aiokafka. The
# W3C ``traceparent``/``tracestate`` propagators produce/consume a str->str map,
# so we adapt between the two representations here.


def inject_trace_context(
    headers: Optional[list[tuple[str, bytes]]] = None,
) -> list[tuple[str, bytes]]:
    """Inject the current trace context into Kafka message headers.

    Returns a header list (aiokafka ``[(key, value_bytes), ...]``) with the W3C
    trace-context headers appended so downstream engine consumers can continue
    the same trace. When the SDK is unavailable the headers are returned
    unchanged (an empty list if none were supplied).
    """
    result: list[tuple[str, bytes]] = list(headers or [])
    if not _OTEL_AVAILABLE:
        return result

    carrier: dict[str, str] = {}
    _otel_inject(carrier)  # type: ignore[misc]
    for key, value in carrier.items():
        result.append((key, value.encode("utf-8")))
    return result


def extract_trace_context(
    headers: Optional[list[tuple[str, bytes]]],
) -> Any:
    """Extract a parent trace context from Kafka message headers.

    Returns an OpenTelemetry ``Context`` suitable for passing as the ``context``
    argument to :func:`start_span` / the ``*_span`` helpers, or ``None`` when the
    SDK is unavailable or no trace headers are present.
    """
    if not _OTEL_AVAILABLE or not headers:
        return None

    carrier: dict[str, str] = {}
    for key, value in headers:
        if isinstance(value, (bytes, bytearray)):
            carrier[key] = bytes(value).decode("utf-8", errors="replace")
        else:  # pragma: no cover - defensive
            carrier[key] = str(value)
    return _otel_extract(carrier)  # type: ignore[misc]


__all__ = [
    # availability / setup
    "configure_tracing",
    "instrument_tracing",
    "get_tracer",
    # PII guard
    "FORBIDDEN_PII_KEYS",
    "PIIViolationError",
    "assert_no_pii",
    "safe_span_attributes",
    "set_span_attributes",
    # span helpers
    "start_span",
    "root_claim_span",
    "engine_span",
    "adapter_span",
    "shap_span",
    "llm_span",
    # Kafka propagation
    "inject_trace_context",
    "extract_trace_context",
]

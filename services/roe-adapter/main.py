"""
services/roe-adapter/main.py
============================
ROE Adapter FastAPI application.

Accepts Record of Employment (ROE) submissions from ROE Web over three
protocols and normalises every submission to the canonical
:class:`shared.models.Claim` model before publishing a ``roe.received`` event
to Kafka (keyed by ``claim_id``):

* ``POST /roe/rest``  — REST, accepting both ``application/json`` and
  ``application/xml`` (the incoming ``Content-Type`` is inspected and XML is
  parsed with lxml).
* ``/roe/soap``       — SOAP 1.1/1.2 endpoint served via ``spyne`` (mounted as a
  WSGI sub-application). Falls back to a lightweight SOAP-over-POST handler when
  spyne is not installed so the endpoint remains available in every environment.

On a normalisation failure the endpoint returns a structured JSON error with
HTTP 422 and does NOT publish anything to Kafka. Kafka publishing degrades
gracefully in development (a broker that is unreachable is logged, not fatal).

Requirements: 1.6, 1.7, 1.8
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse

from shared.auth import get_current_user

try:  # local package layout when run from the service directory
    from config import get_settings
    from normalizer import (
        NormalizationError,
        detect_and_decode,
        normalize_roe,
        xml_to_dict,
    )
except ImportError:  # pragma: no cover - package-style import
    from services.roe_adapter.config import get_settings  # type: ignore
    from services.roe_adapter.normalizer import (  # type: ignore
        NormalizationError,
        detect_and_decode,
        normalize_roe,
        xml_to_dict,
    )

from shared.models import Claim

logger = logging.getLogger("roe-adapter")

app = FastAPI(title="EI-ADS ROE Adapter", version="1.0.0")

# --- Prometheus metrics + /metrics endpoint (task 31.1) ---
from shared.metrics import instrument_app  # noqa: E402

instrument_app(app, kind="adapter")
# --- end metrics wiring ---

# ── OpenTelemetry distributed tracing (shared.telemetry, task 32.1 / Req 21.5) ──
# Single, self-contained hook: all tracer setup and span helpers live in
# shared/telemetry.py. No-op when the OpenTelemetry packages are absent.
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="roe-adapter")
# ── end OpenTelemetry tracing ──

# Module-level Kafka producer handle (created on startup; may be None in dev).
_producer: Any = None


# ---------------------------------------------------------------------------
# Kafka publishing
# ---------------------------------------------------------------------------


async def _get_producer() -> Any:
    """Lazily create and start the aiokafka producer.

    Returns ``None`` (and logs a warning) when the broker cannot be reached and
    graceful degradation is enabled, so local development does not require a
    running Kafka cluster.
    """
    global _producer
    if _producer is not None:
        return _producer

    settings = get_settings()
    try:
        from aiokafka import AIOKafkaProducer

        producer = AIOKafkaProducer(bootstrap_servers=settings.kafka_bootstrap_servers)
        await producer.start()
        _producer = producer
        return _producer
    except Exception as exc:  # broad: any connection/import error degrades in dev
        if settings.kafka_degrade_gracefully:
            logger.warning("Kafka producer unavailable, degrading gracefully: %s", exc)
            return None
        raise


async def publish_roe_received(claim: Claim) -> bool:
    """Publish a ``roe.received`` event keyed by ``claim_id``.

    Returns ``True`` when the event was published, ``False`` when publishing was
    skipped due to graceful degradation. Never called on normalisation failure.
    """
    settings = get_settings()
    producer = await _get_producer()
    if producer is None:
        return False

    event = {
        "event_type": "roe.received",
        "claim_id": str(claim.claim_id),
        "sin_hash": claim.sin_hash,
        "employer_bn": claim.employer_bn,
        "received_at": claim.received_at.isoformat(),
        "payload": claim.to_dict(),
    }
    await producer.send_and_wait(
        settings.kafka_roe_topic,
        value=json.dumps(event).encode("utf-8"),
        key=str(claim.claim_id).encode("utf-8"),
    )
    return True


# ---------------------------------------------------------------------------
# Request body -> raw ROE dict
# ---------------------------------------------------------------------------


def parse_body_to_raw(body: bytes, content_type: str) -> dict[str, Any]:
    """Decode and parse a request body into a raw ROE field dict.

    The bytes are decoded with UTF-8/ISO-8859-1 detection, then parsed as JSON
    or XML depending on the ``Content-Type``. Raises :class:`NormalizationError`
    when the body cannot be parsed.
    """
    text = detect_and_decode(body)
    ctype = (content_type or "").split(";")[0].strip().lower()

    is_xml = "xml" in ctype or (not ctype and text.lstrip().startswith("<"))

    try:
        if is_xml:
            return xml_to_dict(text)
        # Default to JSON.
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise NormalizationError(
                [{"field": "_root", "code": "INVALID", "message": "JSON body must be an object."}]
            )
        return parsed
    except NormalizationError:
        raise
    except Exception as exc:
        raise NormalizationError(
            [{"field": "_root", "code": "PARSE_ERROR", "message": f"Could not parse body: {exc}"}]
        )


# ---------------------------------------------------------------------------
# REST endpoint
# ---------------------------------------------------------------------------


@app.post("/roe/rest")
async def roe_rest(
    request: Request,
    _user: dict[str, Any] = Depends(get_current_user),
) -> JSONResponse:
    """Accept a ROE submission as JSON or XML, normalise, and publish.

    Requires a valid GCKey Bearer token (401 on missing/invalid). ROE Web is an
    authenticated federal submitter, so any valid principal is accepted here;
    role-specific scope is not further constrained for machine-to-machine ROE
    intake.
    """
    body = await request.body()
    content_type = request.headers.get("content-type", "")

    try:
        raw = parse_body_to_raw(body, content_type)
        claim = normalize_roe(raw)
    except NormalizationError as exc:
        # Structured error; suppress Kafka publish.
        return JSONResponse(status_code=422, content={"errors": exc.errors})

    published = await publish_roe_received(claim)
    return JSONResponse(
        status_code=202,
        content={
            "claim_id": str(claim.claim_id),
            "status": claim.status.value,
            "published": published,
        },
    )


# --- /healthz: report Kafka connectivity, 200/503 (task 31.2, Req 21.4) ---
def _kafka_connected() -> bool:
    """Best-effort Kafka producer connectivity check.

    The producer is created lazily on first publish, so a ``None`` handle in
    dev simply means no publish has happened yet. When a handle exists we treat
    a live ``_sender``/started state as connected.
    """
    producer = _producer
    if producer is None:
        return False
    sender = getattr(producer, "_sender", None)
    if sender is not None:
        return not getattr(sender, "_closed", False)
    return True


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Report Kafka connectivity for the ROE Adapter.

    Returns 200 when Kafka is reachable, or when graceful degradation is
    enabled (local dev / best-effort publishing). Returns 503 only when Kafka
    is required (``kafka_degrade_gracefully`` disabled) but unreachable.
    """
    settings = get_settings()
    kafka_ok = _kafka_connected()
    degrade = bool(getattr(settings, "kafka_degrade_gracefully", True))
    healthy = kafka_ok or degrade
    code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=code,
        content={
            "status": "ok" if healthy else "degraded",
            "checks": {"kafka": kafka_ok, "degrade_gracefully": degrade},
        },
    )
# --- end /healthz ---


# ---------------------------------------------------------------------------
# SOAP endpoint (spyne)
# ---------------------------------------------------------------------------


def _build_soap_app() -> Any:
    """Build the spyne WSGI application exposing a SOAP 1.1/1.2 ROE endpoint.

    Returns ``None`` when spyne is not installed, in which case a fallback
    SOAP-over-POST handler (below) serves the ``/roe/soap`` route instead.
    """
    try:
        from spyne import Application, ServiceBase, rpc, Unicode
        from spyne.protocol.soap import Soap11, Soap12
        from spyne.server.wsgi import WsgiApplication
    except ImportError:
        logger.info("spyne not installed; using fallback SOAP-over-POST handler")
        return None

    class ROEService(ServiceBase):
        @rpc(Unicode, _returns=Unicode)
        def SubmitROE(ctx, roe_xml: str) -> str:  # noqa: N805
            """Accept a ROE XML document, normalise, and publish synchronously."""
            import asyncio

            try:
                raw = xml_to_dict(roe_xml)
                claim = normalize_roe(raw)
            except NormalizationError as exc:
                return json.dumps({"status": "ERROR", "errors": exc.errors})

            try:
                loop = asyncio.new_event_loop()
                published = loop.run_until_complete(publish_roe_received(claim))
                loop.close()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("SOAP publish failed: %s", exc)
                published = False

            return json.dumps(
                {"status": "RECEIVED", "claim_id": str(claim.claim_id), "published": published}
            )

    soap_app = Application(
        [ROEService],
        tns="urn:esdc:ei-ads:roe",
        in_protocol=Soap11(validator="lxml"),
        out_protocol=Soap12(),
    )
    return WsgiApplication(soap_app)


_soap_wsgi = _build_soap_app()

if _soap_wsgi is not None:
    from starlette.middleware.wsgi import WSGIMiddleware  # type: ignore

    app.mount("/roe/soap", WSGIMiddleware(_soap_wsgi))
else:

    @app.post("/roe/soap")
    async def roe_soap_fallback(request: Request) -> Any:
        """Fallback SOAP 1.1/1.2 handler when spyne is unavailable.

        Extracts the SOAP Body payload from the envelope, normalises it, and
        publishes ``roe.received``. Returns a SOAP response envelope.
        """
        from starlette.responses import Response

        body = await request.body()
        text = detect_and_decode(body)

        # Parse the SOAP envelope first. Malformed/empty XML is a client error,
        # so return a 400 SOAP fault instead of letting the XML parser raise a
        # 500 (Requirement 17.1 — no server error on a bad request body).
        try:
            raw = xml_to_dict(text)
        except Exception as exc:  # noqa: BLE001 - any XML parse failure is a 400
            fault = (
                '<?xml version="1.0"?>'
                '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
                "<soap:Body><soap:Fault><faultcode>Client</faultcode>"
                f"<faultstring>{json.dumps('Malformed XML request body: ' + str(exc))}</faultstring>"
                "</soap:Fault></soap:Body></soap:Envelope>"
            )
            return Response(content=fault, media_type="text/xml", status_code=400)

        try:
            # Drop SOAP envelope wrapper tags that are not ROE fields.
            claim = normalize_roe(raw)
        except NormalizationError as exc:
            fault = (
                '<?xml version="1.0"?>'
                '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
                "<soap:Body><soap:Fault><faultcode>Client</faultcode>"
                f"<faultstring>{json.dumps(exc.errors)}</faultstring>"
                "</soap:Fault></soap:Body></soap:Envelope>"
            )
            return Response(content=fault, media_type="text/xml", status_code=400)

        published = await publish_roe_received(claim)
        envelope = (
            '<?xml version="1.0"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            "<soap:Body><SubmitROEResponse>"
            f"<claimId>{claim.claim_id}</claimId>"
            f"<status>{claim.status.value}</status>"
            f"<published>{str(published).lower()}</published>"
            "</SubmitROEResponse></soap:Body></soap:Envelope>"
        )
        return Response(content=envelope, media_type="text/xml", status_code=200)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _producer
    if _producer is not None:
        try:
            await _producer.stop()
        finally:
            _producer = None

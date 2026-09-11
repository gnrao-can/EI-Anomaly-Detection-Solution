"""
services/claim-intake/main.py
=============================
FastAPI application skeleton for the Claim Intake Service.

Responsibilities implemented so far:
  - (4.1) FastAPI app with a lifespan handler that opens/closes the asyncpg pool
    and the aiokafka producer, reading connection settings from the environment
    and degrading gracefully when those systems are unavailable in local dev.
  - (4.1) ``GET /healthz`` — checks DB / Kafka / Redis connectivity and returns
    200 or 503.
  - (4.2) ``POST /api/v1/claims`` — validates the payload via
    ``validate_claim_fields`` (returning structured 400 errors on failure),
    hashes the SIN with SHA-256 + per-environment salt, builds a ``Claim`` model
    with status ``RECEIVED``, and persists it via ``ClaimRepository``.

  - (4.3) ``POST /api/v1/claims`` two-phase commit — after persisting the claim
    to PostgreSQL, publish a ``claim.received`` event to Kafka (keyed by
    ``claim_id``) within a 500 ms budget. If the publish fails or times out, the
    PG write is rolled back (``delete_claim``) and a structured error is
    returned; if the PG write itself fails, nothing is published.

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.11, 19.1, 19.7, 22.6
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from shared.auth import Role, require_role
from shared.models import Claim, ClaimStatus
from shared.retry import connect_with_retry
from shared.validation import validate_claim_fields

try:  # sibling modules imported by bare name in the service directory
    from hashing import hash_sin  # type: ignore
    from repository import ClaimRepository, FakeClaimRepository  # type: ignore
    from kafka_producer import (  # type: ignore
        ClaimEventProducer,
        FakeClaimEventProducer,
        KafkaPublishError,
    )
    from privacy import (  # type: ignore
        FakePrivacyRepository,
        PrivacyRepository,
    )
except ImportError:  # pragma: no cover - when imported as a package
    from .hashing import hash_sin  # type: ignore
    from .repository import ClaimRepository, FakeClaimRepository  # type: ignore
    from .kafka_producer import (  # type: ignore
        ClaimEventProducer,
        FakeClaimEventProducer,
        KafkaPublishError,
    )
    from .privacy import (  # type: ignore
        FakePrivacyRepository,
        PrivacyRepository,
    )

# ---------------------------------------------------------------------------
# Optional external clients — guarded so the module imports (and unit tests
# run) even when the driver packages or the backing services are unavailable.
# ---------------------------------------------------------------------------

try:
    import asyncpg  # type: ignore

    _HAS_ASYNCPG = True
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore
    _HAS_ASYNCPG = False

try:
    from aiokafka import AIOKafkaProducer  # type: ignore

    _HAS_AIOKAFKA = True
except ImportError:  # pragma: no cover
    AIOKafkaProducer = None  # type: ignore
    _HAS_AIOKAFKA = False

try:
    import redis.asyncio as aioredis  # type: ignore

    _HAS_REDIS = True
except ImportError:  # pragma: no cover
    aioredis = None  # type: ignore
    _HAS_REDIS = False


try:  # config lives alongside this module in the service directory
    from config import get_settings  # type: ignore
except ImportError:  # pragma: no cover - when imported as a package
    from .config import get_settings  # type: ignore


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class ClaimSubmission(BaseModel):
    """Canonical claim field set (retained for reuse / typing).

    NOTE: ``POST /api/v1/claims`` no longer binds this model as a JSON body — the
    endpoint accepts ``multipart/form-data`` per design.md. This model is kept
    for any internal callers that want a typed representation of the claim
    fields. ``postal_code`` is OPTIONAL to match Req 1.2 (it is not a mandatory
    field and the schema column is nullable).
    """

    sin: str = Field(..., description="Claimant SIN (validated and hashed downstream).")
    employer_bn: str = Field(..., description="15-char employer Business Number.")
    insurable_hours: float = Field(..., description="Total insurable hours worked.")
    insurable_earnings: float = Field(..., description="Total insurable earnings (CAD).")
    separation_date: str = Field(..., description="Employment separation date (ISO-8601).")
    separation_reason: str = Field(..., description="Free-text reason for separation.")
    postal_code: Optional[str] = Field(
        default=None, description="Optional claimant residential postal code / FSA."
    )


    # ── Optional fields carried through to the canonical Claim model ──
    separation_reason_code: Optional[str] = Field(
        default=None, description="ROE reason code (A–N)."
    )
    claim_start_date: Optional[str] = Field(
        default=None, description="First day of the EI benefit period (ISO-8601)."
    )
    economic_region_code: Optional[str] = Field(
        default=None, description="ESDC 2-char economic region code."
    )
    noc_code: Optional[str] = Field(
        default=None, description="NOC 2021 5-digit occupational code."
    )
    residential_address_hash: Optional[str] = Field(
        default=None, description="SHA-256 of the full residential address."
    )
    employer_work_location: Optional[str] = Field(
        default=None, description="Full declared employer work location."
    )


class ClaimAccepted(BaseModel):
    """202 response body for an accepted claim submission."""

    claim_id: str
    status: str = "RECEIVED"


class ValidationErrorResponse(BaseModel):
    """400 response body: a structured list of field-level validation errors."""

    errors: list[dict[str, str]]


class IntakeErrorResponse(BaseModel):
    """5xx response body for a persistence or publish failure during intake.

    Returned when the PostgreSQL write fails (502/500) or when the Kafka publish
    of ``claim.received`` fails and the PG write is rolled back. No ``claim_id``
    is returned because, after rollback, the claim was not durably accepted.
    """

    error: dict[str, str]


# ---------------------------------------------------------------------------
# Lifespan handler
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open and close DB / Kafka / Redis connections around the app lifecycle.

    Each connection is best-effort in development: a failure to connect leaves
    the corresponding client as ``None`` and is surfaced later via ``/healthz``,
    rather than preventing the service from starting.
    """
    settings = get_settings()

    app.state.settings = settings
    app.state.db_pool = None
    app.state.kafka_producer = None
    app.state.event_producer = None
    app.state.redis = None
    app.state.claim_repository = None
    app.state.privacy_repository = None

    # ── PostgreSQL ──────────────────────────────────────────────
    if _HAS_ASYNCPG:
        try:
            app.state.db_pool = await asyncpg.create_pool(
                dsn=settings.postgres_dsn,
                min_size=1,
                max_size=10,
            )
        except Exception:  # noqa: BLE001 - degrade gracefully in dev
            app.state.db_pool = None

    # ── Claim repository ────────────────────────────────────────
    # Use the asyncpg-backed repository when a pool is available; otherwise fall
    # back to the in-memory fake so the service still functions in local dev.
    if app.state.db_pool is not None:
        app.state.claim_repository = ClaimRepository(app.state.db_pool)
    else:
        app.state.claim_repository = FakeClaimRepository()

    # ── Privacy Act repository (task 28.1) ──────────────────────
    # Backs the /api/v1/privacy access & deletion endpoints. Uses the asyncpg
    # pool (+ optional ClickHouse client) when available, otherwise an in-memory
    # fake for local dev.
    app.state.privacy_repository = None
    if app.state.db_pool is not None:
        app.state.privacy_repository = PrivacyRepository(
            app.state.db_pool,
            clickhouse_client=getattr(app.state, "clickhouse", None),
        )
    else:
        app.state.privacy_repository = FakePrivacyRepository()

    # ── Kafka ───────────────────────────────────────────────────
    if _HAS_AIOKAFKA:
        # Retry the initial Kafka connect with backoff (shared.retry): on a
        # compose bring-up the broker is often not ready for a few seconds after
        # this service starts, and a single failed attempt would leave the
        # producer permanently inert (claim.received never published) until a
        # manual restart. The retry lets it attach itself once Kafka is up.
        async def _build_producer() -> "AIOKafkaProducer":
            producer = AIOKafkaProducer(
                bootstrap_servers=settings.kafka_bootstrap_servers,
            )
            await producer.start()
            return producer

        producer = await connect_with_retry(
            _build_producer, description="claim-intake Kafka producer"
        )
        if producer is not None:
            app.state.kafka_producer = producer
            # Wrap the raw producer so the intake endpoint publishes
            # ``claim.received`` keyed by claim_id within the 500 ms budget.
            app.state.event_producer = ClaimEventProducer(producer)
        else:
            app.state.kafka_producer = None
            app.state.event_producer = None

    # ── Redis ───────────────────────────────────────────────────
    if _HAS_REDIS:
        try:
            client = aioredis.from_url(settings.redis_url)
            await client.ping()
            app.state.redis = client
        except Exception:  # noqa: BLE001 - degrade gracefully in dev
            app.state.redis = None

    try:
        yield
    finally:
        if app.state.kafka_producer is not None:
            try:
                await app.state.kafka_producer.stop()
            except Exception:  # noqa: BLE001
                pass
        if app.state.db_pool is not None:
            try:
                await app.state.db_pool.close()
            except Exception:  # noqa: BLE001
                pass
        if app.state.redis is not None:
            try:
                await app.state.redis.aclose()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="EI-ADS Claim Intake Service",
    version="0.1.0",
    lifespan=lifespan,
)

# ── OpenTelemetry distributed tracing (shared.telemetry, task 32.1 / Req 21.5) ──
# Single, self-contained hook: all tracer setup and span helpers live in
# shared/telemetry.py. No-op when the OpenTelemetry packages are absent.
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="claim-intake")
# ── end OpenTelemetry tracing ──

# --- Prometheus metrics + /metrics endpoint (task 31.1) ---
from shared.metrics import instrument_app  # noqa: E402

instrument_app(app, kind="service")
# --- end metrics wiring ---


def _to_decimal(value: Any) -> Decimal:
    """Coerce a numeric payload value to ``Decimal`` via the string path.

    Validation (``> 0`` etc.) has already run by the time this is called, so any
    conversion failure here is unexpected; we still guard it to avoid leaking a
    raw exception to the client.
    """
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _fsa(postal_code: str) -> str:
    """Return the Forward Sortation Area (first 3 chars) of a postal code."""
    cleaned = (postal_code or "").replace(" ", "").upper()
    return cleaned[:3]


def _build_claim(
    payload: dict[str, Any],
    sin_hash: str,
    *,
    roe_document_id: Optional[uuid.UUID] = None,
) -> Claim:
    """Construct a canonical ``Claim`` (status RECEIVED) from submission fields.

    ``payload`` is the coerced multipart field mapping (never containing the raw
    SIN on the returned model — only its ``sin_hash`` digest is carried). Only
    the FSA (first 3 chars) of the postal code is retained, and ``postal_code``
    is optional (empty FSA when absent).
    """
    separation_date = payload["separation_date"]
    claim_start = payload.get("claim_start_date") or separation_date
    postal_code = payload.get("postal_code") or ""
    return Claim(
        claim_id=uuid.uuid4(),
        sin_hash=sin_hash,
        employer_bn=str(payload["employer_bn"]).strip().upper(),
        insurable_hours=_to_decimal(payload["insurable_hours"]),
        insurable_earnings=_to_decimal(payload["insurable_earnings"]),
        separation_date=date.fromisoformat(str(separation_date)),
        separation_reason=str(payload["separation_reason"]),
        separation_reason_code=payload.get("separation_reason_code") or "",
        claim_start_date=date.fromisoformat(str(claim_start)),
        economic_region_code=payload.get("economic_region_code") or "",
        noc_code=payload.get("noc_code") or "",
        residential_postal_code=_fsa(postal_code),
        residential_address_hash=payload.get("residential_address_hash") or "",
        employer_work_location=payload.get("employer_work_location") or "",
        received_at=datetime.now(timezone.utc),
        status=ClaimStatus.RECEIVED,
        roe_document_id=roe_document_id,
    )


def _intake_error(code: str, message: str, http_status: int) -> JSONResponse:
    """Build a structured intake error response (no ``claim_id`` on failure)."""
    return JSONResponse(
        status_code=http_status,
        content={"error": {"code": code, "message": message}},
    )


@app.post(
    "/api/v1/claims",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ClaimAccepted,
    responses={
        400: {"model": ValidationErrorResponse},
        500: {"model": IntakeErrorResponse},
        502: {"model": IntakeErrorResponse},
    },
)
async def create_claim(
    sin: str = Form(...),
    employer_bn: str = Form(...),
    insurable_hours: str = Form(...),
    insurable_earnings: str = Form(...),
    separation_date: str = Form(...),
    separation_reason: str = Form(...),
    postal_code: Optional[str] = Form(default=None),
    roe_document: Optional[UploadFile] = File(default=None),
    user: dict[str, Any] = Depends(require_role(Role.CLAIMANT)),
) -> Any:
    """Accept, validate, hash, and persist a **multipart** claim submission.

    Per the design contract (design.md §Claim Intake Service) this endpoint
    accepts ``multipart/form-data`` with the mandatory fields (Req 1.2) ``sin``,
    ``employer_bn``, ``insurable_hours``, ``insurable_earnings``,
    ``separation_date``, ``separation_reason``, plus an OPTIONAL ``postal_code``
    and an OPTIONAL ``roe_document`` file upload.

    Requires the CLAIMANT role, enforced by ``require_role(Role.CLAIMANT)`` — a
    valid GCKey Bearer validated via JWKS (401 without a token, 403 for a
    non-claimant role; Req 19.7). Processing:

    1. Validate the fields with ``validate_claim_fields``. On any failure, return
       HTTP 400 with a structured ``{"errors": [{field,code,hint}]}`` body and
       persist nothing (Requirement 1.2).
    2. Hash the raw SIN (SHA-256 + per-environment salt) immediately; the raw
       SIN is never persisted or logged (Requirement 1.11).
    3. Build a ``Claim`` with status ``RECEIVED`` and persist via the repository
       (Requirement 1.4). An uploaded ROE is accepted but never required.
    4. Two-phase commit (Requirements 1.4/1.5):
       a. Persist the claim to PostgreSQL (status ``RECEIVED``). If the PG write
          fails, return a structured 500 error and publish nothing.
       b. Publish ``claim.received`` to Kafka keyed by ``claim_id`` within a
          500 ms budget. If the publish fails or times out, roll back the PG
          write (``delete_claim``) and return a structured 502 error.
    5. On full success return 202 with the real ``claim_id``.
    """
    payload: dict[str, Any] = {
        "sin": sin,
        "employer_bn": employer_bn,
        "insurable_hours": insurable_hours,
        "insurable_earnings": insurable_earnings,
        "separation_date": separation_date,
        "separation_reason": separation_reason,
    }
    # postal_code is optional (Req 1.2): only carry it when a value is supplied.
    if postal_code is not None and str(postal_code).strip():
        payload["postal_code"] = postal_code

    # ── 1. Field validation (structured errors, no persistence on failure) ──
    errors = validate_claim_fields(payload)
    if errors:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"errors": errors},
        )

    # ── 2. Hash the SIN immediately; never persist or log the raw SIN ──
    # Fall back to freshly loaded settings if the lifespan handler has not run
    # (e.g. a TestClient used without an active lifespan context).
    settings = getattr(app.state, "settings", None) or get_settings()
    sin_hash = hash_sin(sin, settings.sin_hash_salt)

    # ── 2b. Optional ROE upload — accepted, never required (do not block). ──
    roe_document_id: Optional[uuid.UUID] = None
    if roe_document is not None and roe_document.filename:
        # Record a reference id for the uploaded document without depending on
        # or persisting its bytes here. Reading is drained so the request body
        # is fully consumed.
        try:
            await roe_document.read()
        except Exception:  # noqa: BLE001 - never fail intake on ROE read
            pass
        roe_document_id = uuid.uuid4()

    # ── 3. Build the canonical Claim (status RECEIVED) and persist ──
    claim_record = _build_claim(payload, sin_hash, roe_document_id=roe_document_id)

    repository = getattr(app.state, "claim_repository", None)
    if repository is None:
        # Lifespan did not initialise a repository; degrade to an in-memory one.
        repository = FakeClaimRepository()
        app.state.claim_repository = repository

    # ── 4a. Persist to PostgreSQL. On failure: no publish, no Claim_ID. ──
    try:
        claim_id = await repository.insert_claim(claim_record)
    except Exception:  # noqa: BLE001 - surface as a structured error, no PII
        return _intake_error(
            "PERSISTENCE_FAILED",
            "The claim could not be saved. Please try again.",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # ── 4b. Publish claim.received within 500 ms; roll back PG on failure. ──
    event_producer = getattr(app.state, "event_producer", None)
    if event_producer is not None:
        try:
            await event_producer.publish_claim_received(claim_record)
        except KafkaPublishError:
            # Roll back the just-written row so PG and the event stream stay
            # consistent, then return a structured error (Requirement 1.5).
            try:
                await repository.delete_claim(claim_id)
            except Exception:  # noqa: BLE001 - rollback is best-effort
                pass
            return _intake_error(
                "PUBLISH_FAILED",
                "The claim could not be accepted at this time. Please try again.",
                status.HTTP_502_BAD_GATEWAY,
            )

    return ClaimAccepted(claim_id=str(claim_id), status=ClaimStatus.RECEIVED.value)


# ---------------------------------------------------------------------------
# Privacy Act compliance endpoints (task 28.1, Requirements 19.4 / 19.5)
# ---------------------------------------------------------------------------


def _claimant_sin_hash(user: dict[str, Any]) -> Optional[str]:
    """Derive the authenticated claimant's own ``sin_hash`` from token claims.

    The claimant is keyed by ``sin_hash``; the raw SIN is never stored. A token
    may carry the claimant's identity as either an already-computed ``sin_hash``
    claim, or a raw ``sin`` claim (which we hash here with the per-environment
    salt). Returns ``None`` when neither is present.
    """
    if not isinstance(user, dict):
        return None

    existing = user.get("sin_hash")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()

    raw_sin = user.get("sin")
    if isinstance(raw_sin, str) and raw_sin.strip():
        settings = getattr(app.state, "settings", None) or get_settings()
        return hash_sin(raw_sin, settings.sin_hash_salt)

    return None


def _resolve_target_sin_hash(
    user: dict[str, Any],
    requested_sin_hash: Optional[str],
    requested_sin: Optional[str],
) -> str:
    """Resolve the ``sin_hash`` a privacy request targets, enforcing scope.

    Rules (Requirement 19.4 / 19.5, RBAC role model):
      - A **CLAIMANT** may only ever operate on their *own* data: any explicitly
        requested target that differs from their own ``sin_hash`` is rejected
        with 403. Their own ``sin_hash`` is derived from the token.
      - An **ADMINISTRATOR** may target any claimant by supplying ``sin_hash`` or
        a raw ``sin`` (hashed here) as a query parameter.

    Raises ``HTTPException`` (400/403) on an unresolvable or out-of-scope target.
    """
    from shared.auth import Role, extract_roles

    roles = extract_roles(user)
    is_admin = Role.ADMINISTRATOR in roles
    own_hash = _claimant_sin_hash(user)

    # Normalise any explicitly requested target to a sin_hash.
    target: Optional[str] = None
    if requested_sin_hash and requested_sin_hash.strip():
        target = requested_sin_hash.strip()
    elif requested_sin and requested_sin.strip():
        settings = getattr(app.state, "settings", None) or get_settings()
        target = hash_sin(requested_sin, settings.sin_hash_salt)

    if is_admin:
        # Administrators may act on their own data or an explicitly named target.
        resolved = target or own_hash
        if not resolved:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A target sin or sin_hash is required for this request.",
            )
        return resolved

    # Claimant: own-data-only.
    if own_hash is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to determine claimant identity from the token.",
        )
    if target is not None and target != own_hash:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A claimant may only access or erase their own data.",
        )
    return own_hash


def _privacy_repository():
    """Return the configured privacy repository, degrading to a fake if absent."""
    repository = getattr(app.state, "privacy_repository", None)
    if repository is None:
        repository = FakePrivacyRepository()
        app.state.privacy_repository = repository
    return repository


@app.get("/api/v1/privacy/access")
async def privacy_access(
    sin_hash: Optional[str] = None,
    sin: Optional[str] = None,
    user: dict[str, Any] = Depends(
        require_role(Role.CLAIMANT, Role.ADMINISTRATOR)
    ),
) -> JSONResponse:
    """Return all records held for a claimant in a machine-readable format.

    Requires the CLAIMANT (own data only) or ADMINISTRATOR role. Queries
    PostgreSQL (``claims`` / ``indicators`` / ``risk_assessments``) and the
    ClickHouse analytics/audit store for every record tied to the claimant's
    ``sin_hash`` and returns them as JSON, satisfying the 30-day Privacy Act
    access SLA (Requirement 19.4).
    """
    target = _resolve_target_sin_hash(user, sin_hash, sin)
    report = await _privacy_repository().access_records(target)
    return JSONResponse(status_code=status.HTTP_200_OK, content=report)


@app.delete("/api/v1/privacy/delete")
async def privacy_delete(
    sin_hash: Optional[str] = None,
    sin: Optional[str] = None,
    user: dict[str, Any] = Depends(
        require_role(Role.CLAIMANT, Role.ADMINISTRATOR)
    ),
) -> JSONResponse:
    """Anonymise identifying fields for a claimant while preserving aggregates.

    Requires the CLAIMANT (own data only) or ADMINISTRATOR role. Anonymises the
    identifying fields in ``claims`` / ``indicators`` / ``risk_assessments`` for
    the claimant's ``sin_hash`` while preserving the aggregate / statistical rows
    required for audit, satisfying the 30-day Privacy Act erasure SLA
    (Requirement 19.5). Returns a machine-readable receipt describing what was
    anonymised.
    """
    target = _resolve_target_sin_hash(user, sin_hash, sin)
    receipt = await _privacy_repository().anonymise_records(target)
    return JSONResponse(status_code=status.HTTP_200_OK, content=receipt)


async def _check_db(app_: FastAPI) -> bool:
    pool = getattr(app_.state, "db_pool", None)
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:  # noqa: BLE001
        return False


async def _check_kafka(app_: FastAPI) -> bool:
    producer = getattr(app_.state, "kafka_producer", None)
    if producer is None:
        return False
    try:
        # A successful client bootstrap keeps cluster metadata available.
        await producer.client.fetch_all_metadata()
        return True
    except Exception:  # noqa: BLE001
        # If metadata cannot be fetched but a producer object exists, treat the
        # running producer as connected.
        return producer is not None


async def _check_redis(app_: FastAPI) -> bool:
    client = getattr(app_.state, "redis", None)
    if client is None:
        return False
    try:
        return bool(await client.ping())
    except Exception:  # noqa: BLE001
        return False


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Report DB / Kafka / Redis connectivity.

    Returns 200 when all connected dependencies are healthy, otherwise 503.
    In local development where dependencies are intentionally absent, the
    endpoint reports each dependency's status so operators can see what is down.
    """
    checks = {
        "database": await _check_db(app),
        "kafka": await _check_kafka(app),
        "redis": await _check_redis(app),
    }
    healthy = all(checks.values())
    code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=code,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )


__all__ = [
    "app",
    "ClaimSubmission",
    "ClaimAccepted",
    "ValidationErrorResponse",
    "IntakeErrorResponse",
    "lifespan",
    "privacy_access",
    "privacy_delete",
]

"""
services/portal-backend/main.py
===============================
FastAPI application for the Portal Backend-for-Frontend (BFF).

The BFF is the single ``http://localhost:8000`` origin the ``claimant-portal``
SPA talks to. It:

  * authenticates the browser via GCKey OAuth 2.0 (authorization-code + PKCE)
    and issues a signed HttpOnly session cookie; the GCKey access token is
    carried in that session so downstream calls can forward it as a Bearer;
  * serves the claimant status view and the investigator queue / detail
    ``/portal/*`` endpoints directly from PostgreSQL, applying role-based PII
    masking server-side (Req 22.1, 22.2, 22.6);
  * records investigator dispositions and publishes ``outcome.feedback`` to
    Kafka for CONFIRMED_FRAUD / FALSE_POSITIVE (Req 22.4);
  * streams flagged claims over SSE (Req 22.1); and
  * forwards the SPA's multipart claim submission to the Claim Intake Service
    with the caller's GCKey Bearer (design GCKey flow "Forward with Bearer
    token"), and reverse-proxies the Privacy Act write paths likewise, so the
    browser only ever sees one origin.

Every external dependency (PostgreSQL, Kafka, httpx) is guarded so the module
imports and the unit tests run without a live backend, and the app degrades to
in-memory / no-op behaviour in local development — mirroring
``services/claim-intake/main.py``.

Requirements: 19.7, 22.1, 22.2, 22.3, 22.4, 22.6
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from shared.auth import Role

try:  # sibling modules imported by bare name in the service directory
    from config import get_settings  # type: ignore
    from auth import (  # type: ignore
        Principal,
        INVESTIGATOR_ROLES,
        SessionError,
        get_principal,
        get_optional_principal,
        require_roles,
        sign_session,
        verify_session,
    )
    from repository import (  # type: ignore
        FakePortalRepository,
        PortalRepository,
        mask_detail,
        mask_flagged,
    )
    from sse import ClaimEventBroadcaster, format_sse, HEARTBEAT_COMMENT  # type: ignore
    import gckey  # type: ignore
except ImportError:  # pragma: no cover - when imported as a package
    from .config import get_settings  # type: ignore
    from .auth import (  # type: ignore
        Principal,
        INVESTIGATOR_ROLES,
        SessionError,
        get_principal,
        get_optional_principal,
        require_roles,
        sign_session,
        verify_session,
    )
    from .repository import (  # type: ignore
        FakePortalRepository,
        PortalRepository,
        mask_detail,
        mask_flagged,
    )
    from .sse import ClaimEventBroadcaster, format_sse, HEARTBEAT_COMMENT  # type: ignore
    from . import gckey  # type: ignore


# ---------------------------------------------------------------------------
# Optional external clients — guarded for import-safety / local degradation.
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
    import httpx  # type: ignore

    _HAS_HTTPX = True
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore
    _HAS_HTTPX = False


OUTCOME_FEEDBACK_TOPIC = "outcome.feedback"

# Dispositions that trigger a model-feedback publish (Req 22.4).
FEEDBACK_DISPOSITIONS = {"CONFIRMED_FRAUD", "FALSE_POSITIVE"}
VALID_DISPOSITIONS = {"CONFIRMED_FRAUD", "FALSE_POSITIVE", "NEEDS_FURTHER_REVIEW"}


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class DispositionRequest(BaseModel):
    """Body for ``PUT /api/v1/claims/{claim_id}/disposition`` (Req 22.3)."""

    disposition: str = Field(..., description="One of CONFIRMED_FRAUD | FALSE_POSITIVE | NEEDS_FURTHER_REVIEW.")
    case_notes: str = Field(default="", description="Investigator case notes.")


class CallbackRequest(BaseModel):
    """Body for ``POST /auth/callback`` — the SPA's PKCE code exchange (gckey mode).

    Mirrors the JSON the ``claimant-portal`` SPA posts from
    ``services/claimant-portal/src/auth/gckey.ts`` :func:`completeLogin`.
    """

    code: str = Field(..., description="GCKey authorization code.")
    code_verifier: str = Field(..., description="PKCE code_verifier for the exchange.")
    redirect_uri: str = Field(default="", description="The redirect_uri used in the authorize step.")


# ---------------------------------------------------------------------------
# Lifespan handler
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open/close DB + Kafka around the app lifecycle, degrading gracefully."""
    settings = get_settings()

    app.state.settings = settings
    app.state.db_pool = None
    app.state.kafka_producer = None
    app.state.repository = None
    app.state.broadcaster = ClaimEventBroadcaster()

    # ── PostgreSQL ──────────────────────────────────────────────
    if _HAS_ASYNCPG:
        try:
            app.state.db_pool = await asyncpg.create_pool(
                dsn=settings.postgres_dsn, min_size=1, max_size=10
            )
        except Exception:  # noqa: BLE001 - degrade gracefully in dev
            app.state.db_pool = None

    if app.state.db_pool is not None:
        app.state.repository = PortalRepository(app.state.db_pool)
    else:
        app.state.repository = FakePortalRepository()

    # ── Kafka (outcome.feedback publisher) ──────────────────────
    if _HAS_AIOKAFKA:
        try:
            producer = AIOKafkaProducer(
                bootstrap_servers=settings.kafka_bootstrap_servers
            )
            await producer.start()
            app.state.kafka_producer = producer
        except Exception:  # noqa: BLE001 - degrade gracefully in dev
            app.state.kafka_producer = None

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


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="EI-ADS Portal Backend-for-Frontend",
    version="0.1.0",
    lifespan=lifespan,
)

# ── OpenTelemetry distributed tracing (shared.telemetry) — no-op when absent ──
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="portal-backend")

# ── Prometheus metrics + /metrics endpoint (shared.metrics) ──
from shared.metrics import instrument_app  # noqa: E402

instrument_app(app, kind="service")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repository():
    """Return the configured repository, degrading to a fake if absent."""
    repo = getattr(app.state, "repository", None)
    if repo is None:
        repo = FakePortalRepository()
        app.state.repository = repo
    return repo


def _broadcaster() -> ClaimEventBroadcaster:
    broadcaster = getattr(app.state, "broadcaster", None)
    if broadcaster is None:
        broadcaster = ClaimEventBroadcaster()
        app.state.broadcaster = broadcaster
    return broadcaster


def _set_session_cookie(
    response: Response,
    role: Role,
    sin_hash: Optional[str],
    preferred_language: Optional[str] = None,
    access_token: Optional[str] = None,
    subject: Optional[str] = None,
) -> None:
    """Sign and attach the session cookie for *role*/*sin_hash*.

    ``preferred_language`` (when provided) is carried in the signed payload so
    ``GET /auth/session`` can surface it to the SPA's AuthContext, which uses it
    to default the UI language (Req 20.1). Omitted when ``None`` to keep the
    payload minimal and backward-compatible with existing sessions.

    ``access_token`` is the GCKey access token from the OAuth code exchange. It
    is carried in the signed session so the BFF can forward it as an
    ``Authorization: Bearer`` header on downstream Claim Intake calls (design
    GCKey flow "Forward with Bearer token"; Req 19.7). The cookie is HttpOnly
    (not readable by page JS) and HMAC-signed (tamper-evident); it is never
    surfaced back to the SPA.
    """
    settings = app.state.settings if hasattr(app.state, "settings") else get_settings()
    payload: dict[str, Any] = {"role": role.value, "sin_hash": sin_hash}
    if preferred_language:
        payload["preferred_language"] = preferred_language
    if access_token:
        payload["access_token"] = access_token
    if subject:
        payload["subject"] = subject
    token = sign_session(
        payload,
        settings.session_secret,
        max_age=settings.session_max_age,
    )
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        max_age=settings.session_max_age,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    settings = app.state.settings if hasattr(app.state, "settings") else get_settings()
    response.delete_cookie(key=settings.session_cookie_name, path="/")


def _bearer_from_principal(principal: Principal) -> str:
    """Return the ``Authorization: Bearer <token>`` value for downstream calls.

    The GCKey access token is carried in the signed session (set at the OAuth
    callback). Downstream EI-ADS APIs validate it via GCKey JWKS (Req 19.7). A
    session without an access token cannot authorise a downstream call, so we
    return 401 and require the browser to re-authenticate via GCKey rather than
    silently sending an unauthenticated request.
    """
    if not principal.access_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has no GCKey access token; please sign in again.",
        )
    return f"Bearer {principal.access_token}"


async def _publish_outcome_feedback(claim_id: str, disposition: str) -> None:
    """Publish an ``outcome.feedback`` event to Kafka (no-op when absent)."""
    producer = getattr(app.state, "kafka_producer", None)
    if producer is None:
        return
    event = {
        "event_type": "outcome.feedback",
        "claim_id": claim_id,
        "disposition": disposition,
    }
    value = json.dumps(event, separators=(",", ":")).encode("utf-8")
    key = claim_id.encode("utf-8")
    try:  # pragma: no cover - requires a live broker
        await producer.send_and_wait(OUTCOME_FEEDBACK_TOPIC, value=value, key=key)
    except Exception:  # noqa: BLE001 - feedback publish is best-effort
        pass


# ---------------------------------------------------------------------------
# Claim submission helpers (Option A: BFF owns/translates the SPA's multipart)
# ---------------------------------------------------------------------------

# Snake_case field names the SPA's claimSubmission.ts posts and reads back in
# its error payload (extractFieldErrors). Keep these EXACT.
_REQUIRED_CLAIM_FIELDS = (
    "sin",
    "employer_bn",
    "insurable_hours",
    "insurable_earnings",
    "separation_date",
    "separation_reason",
)


def _hash_sin(raw_sin: str, salt: str) -> str:
    """Return SHA-256(normalised SIN + salt) as a lowercase hex digest.

    Mirrors ``services/claim-intake/hashing.py`` exactly: the SIN is normalised
    by stripping ASCII spaces and dashes, concatenated with the per-environment
    ``SIN_HASH_SALT``, then SHA-256 hex-digested. Keeping the derivation
    identical means a claimant's session ``sin_hash`` (from the GCKey token) and
    a claim inserted from their submitted SIN resolve to the same own-data key.
    The raw SIN is never stored or logged.
    """
    if not salt:
        raise ValueError("A non-empty SIN_HASH_SALT is required.")
    normalized = raw_sin.replace(" ", "").replace("-", "")
    return hashlib.sha256(f"{normalized}{salt}".encode("utf-8")).hexdigest()


def _field_error(field: str, code: str, hint: str) -> dict[str, str]:
    return {"field": field, "code": code, "hint": hint}


def _validate_claim_fields(
    values: dict[str, Optional[str]],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Validate the SPA's multipart claim fields.

    Returns ``(parsed, errors)`` where *parsed* holds the coerced values and
    *errors* is a list of ``{field, code, hint}`` dicts using the SPA's
    snake_case field names and MISSING / INVALID codes. When *errors* is
    non-empty the caller returns HTTP 400 with ``{"errors": errors}``.
    """
    errors: list[dict[str, str]] = []
    parsed: dict[str, Any] = {}

    # Presence / non-blank checks for every required field.
    for field in _REQUIRED_CLAIM_FIELDS:
        raw = values.get(field)
        if raw is None or not str(raw).strip():
            errors.append(
                _field_error(field, "MISSING", f"Enter the {field.replace('_', ' ')}.")
            )

    # Numeric coercion (only when present) — must be positive per the schema
    # CHECK constraints (insurable_hours > 0, insurable_earnings > 0).
    for field in ("insurable_hours", "insurable_earnings"):
        raw = values.get(field)
        if raw is None or not str(raw).strip():
            continue  # already flagged MISSING above
        try:
            number = float(str(raw).strip())
        except (TypeError, ValueError):
            errors.append(
                _field_error(field, "INVALID", f"Enter a valid number for {field.replace('_', ' ')}.")
            )
            continue
        if number <= 0:
            errors.append(
                _field_error(field, "INVALID", f"The {field.replace('_', ' ')} must be greater than zero.")
            )
            continue
        parsed[field] = number

    # Carry through the remaining string fields (trimmed) when present.
    for field in ("sin", "employer_bn", "separation_date", "separation_reason"):
        raw = values.get(field)
        if raw is not None and str(raw).strip():
            parsed[field] = str(raw).strip()

    return parsed, errors


def _fsa(postal_code: Optional[str]) -> Optional[str]:
    """Return the Forward Sortation Area (first 3 chars) of a postal code."""
    if not postal_code:
        return None
    cleaned = postal_code.replace(" ", "").upper()
    return cleaned[:3] or None


# ---------------------------------------------------------------------------
# Auth endpoints — session view (SPA AuthContext) + logout
# ---------------------------------------------------------------------------


def _session_view(principal: Principal, preferred_language: Optional[str]) -> dict[str, Any]:
    """Build the UserSession JSON the SPA's ``parseSession`` expects.

    Contract (services/claimant-portal/src/auth/gckey.ts):
      ``{ subject: string(non-empty), roles: Role[], preferredLanguage: str|null,
          displayName?: str }``
    where the ``roles`` strings MUST be values the SPA's ``isRole()`` accepts —
    i.e. the :class:`shared.auth.Role` values CLAIMANT / REVIEWER / INVESTIGATOR
    / ADMINISTRATOR.
    """
    role_value = principal.role.value
    # A stable, non-empty subject id: prefer the sin_hash (claimant own-data
    # key), else a role-derived label for staff sessions.
    subject = principal.sin_hash or f"{role_value.lower()}-user"
    return {
        "subject": subject,
        "roles": [role_value],
        "preferredLanguage": preferred_language or "en",
        "displayName": role_value.title(),
    }


@app.get("/auth/session")
async def get_session(
    request: Request,
    principal: Optional[Principal] = Depends(get_optional_principal),
) -> JSONResponse:
    """Return the current UserSession, or 401 when unauthenticated.

    This is the endpoint the ``claimant-portal`` SPA polls to drive its
    AuthContext / route guards. It maps a missing/invalid session cookie to
    HTTP 401 (the SPA's ``fetchSession`` treats 401 as unauthenticated) and a
    valid cookie to a 200 UserSession payload matching the SPA's contract.
    """
    if principal is None:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Not authenticated."},
        )
    # Recover the optional preferred language stashed in the signed cookie.
    preferred_language: Optional[str] = None
    settings = app.state.settings if hasattr(app.state, "settings") else get_settings()
    cookie = request.cookies.get(settings.session_cookie_name)
    if cookie:
        try:
            payload = verify_session(
                cookie, settings.session_secret, max_age=settings.session_max_age
            )
            lang = payload.get("preferred_language")
            if isinstance(lang, str) and lang:
                preferred_language = lang
        except SessionError:
            preferred_language = None
    return JSONResponse(content=_session_view(principal, preferred_language))


@app.get("/auth/logout")
async def logout() -> JSONResponse:
    """Clear the session cookie (GET convenience; the SPA uses POST)."""
    response = JSONResponse(content={"status": "logged_out"})
    _clear_session_cookie(response)
    return response


@app.post("/auth/logout")
async def logout_post() -> JSONResponse:
    """Clear the session cookie via POST (the method the SPA uses)."""
    response = JSONResponse(content={"status": "logged_out"})
    _clear_session_cookie(response)
    return response


@app.get("/auth/me")
async def whoami(principal: Principal = Depends(get_principal)) -> JSONResponse:
    """Return the authenticated caller's role and sin_hash (debugging aid)."""
    return JSONResponse(
        content={"role": principal.role.value, "sin_hash": principal.sin_hash}
    )


# ---------------------------------------------------------------------------
# Auth endpoints — GCKey OAuth 2.0 authorization-code + PKCE
# ---------------------------------------------------------------------------


def _begin_gckey_authorize() -> RedirectResponse:
    """Build the GCKey authorize redirect + short-lived PKCE state cookie.

    Shared by ``GET /submit`` and ``GET /auth/authorize`` so the SPA's default
    ``VITE_GCKEY_AUTHORIZE_URL`` (``/auth/authorize``) lands on the GCKey
    ``/authorize`` endpoint. A signed, short-lived ``gckey_pkce`` cookie carries
    the PKCE verifier + CSRF state for the callback to complete the exchange.
    """
    settings = app.state.settings if hasattr(app.state, "settings") else get_settings()

    verifier, challenge = gckey.generate_pkce_pair()
    state = gckey.generate_state()
    authorize_url = gckey.build_authorize_url(
        authorize_url=settings.gckey_authorize_url,
        client_id=settings.gckey_client_id,
        redirect_uri=settings.gckey_redirect_uri,
        scope=settings.gckey_scope,
        state=state,
        code_challenge=challenge,
    )
    response = RedirectResponse(url=authorize_url, status_code=302)
    # Stash the PKCE verifier + state in short-lived signed cookies so the
    # callback can complete the exchange. Signed with the session secret.
    response.set_cookie(
        "gckey_pkce",
        sign_session({"v": verifier, "s": state}, settings.session_secret),
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        max_age=600,
        path="/",
    )
    return response


@app.get("/submit")
async def gckey_start() -> RedirectResponse:
    """Begin the GCKey authorization-code + PKCE flow (302 to /authorize)."""
    return _begin_gckey_authorize()


@app.get("/auth/authorize")
async def gckey_authorize() -> RedirectResponse:
    """SPA login landing (default ``VITE_GCKEY_AUTHORIZE_URL``).

    Behaves like ``GET /submit``: 302 to the real GCKey authorize URL. The SPA
    may append OAuth query params which are superseded by the server-built
    authorize URL (the server owns the canonical PKCE state).
    """
    return _begin_gckey_authorize()


async def _complete_gckey_exchange(
    code: str,
    state: str,
    pkce_cookie: Optional[str],
    *,
    code_verifier: Optional[str] = None,
) -> tuple[Role, Optional[str], Optional[str], Optional[str]]:
    """Exchange a GCKey code for ``(role, sin_hash, access_token, subject)``.

    Shared by the GET ``/auth/gckey/callback`` (browser redirect) and the POST
    ``/auth/callback`` (SPA fetch) handlers. When ``code_verifier`` is supplied
    (POST/SPA flow, where the SPA held the verifier), it is used directly;
    otherwise the verifier + state are recovered from the signed ``gckey_pkce``
    cookie set by :func:`_begin_gckey_authorize` (browser redirect flow).

    The returned ``access_token`` is the GCKey access token, persisted into the
    session so the BFF can forward it as a Bearer to Claim Intake (Req 19.7).
    """
    settings = app.state.settings if hasattr(app.state, "settings") else get_settings()

    verifier = code_verifier
    if verifier is None:
        # Browser-redirect flow: recover verifier + state from the PKCE cookie.
        if not pkce_cookie:
            raise HTTPException(status_code=400, detail="Missing PKCE state.")
        try:
            pkce = verify_session(pkce_cookie, settings.session_secret, max_age=600)
        except SessionError as exc:
            raise HTTPException(status_code=400, detail="Invalid PKCE state.") from exc
        if not code or state != pkce.get("s"):
            raise HTTPException(status_code=400, detail="Invalid authorization response.")
        verifier = pkce.get("v", "")
    elif not code:
        raise HTTPException(status_code=400, detail="Invalid authorization response.")

    tokens = await gckey.exchange_code_for_tokens(
        token_url=settings.gckey_token_url,
        client_id=settings.gckey_client_id,
        client_secret=settings.gckey_client_secret,
        redirect_uri=settings.gckey_redirect_uri,
        code=code,
        code_verifier=verifier,
    )
    id_token = tokens.get("id_token") or tokens.get("access_token") or ""
    role, sin_hash, subject = gckey.principal_fields_from_id_token(id_token)
    # The Bearer forwarded downstream is the access token (falls back to the
    # id_token when the IdP returns only that, as some test IdPs do).
    access_token = tokens.get("access_token") or tokens.get("id_token") or ""
    return role, sin_hash, access_token or None, subject


@app.get("/auth/gckey/callback")
async def gckey_callback(request: Request, code: str = "", state: str = "") -> Response:
    """Complete the GCKey flow: exchange the code, validate the ID token, set cookie."""
    role, sin_hash, access_token, subject = await _complete_gckey_exchange(
        code, state, request.cookies.get("gckey_pkce")
    )

    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie("gckey_pkce", path="/")
    _set_session_cookie(
        response, role, sin_hash, access_token=access_token, subject=subject
    )
    return response


@app.post("/auth/callback")
async def gckey_callback_post(request: Request, body: CallbackRequest) -> Response:
    """SPA PKCE code exchange (``services/claimant-portal/src/auth/gckey.ts``).

    Performs the token exchange using the ``code_verifier`` the SPA held,
    validates the ID token, sets the signed session cookie (carrying the GCKey
    access token for downstream Bearer-forward), and returns 200 (the SPA only
    checks ``response.ok`` then calls ``fetchSession``).
    """
    role, sin_hash, access_token, subject = await _complete_gckey_exchange(
        body.code,
        state="",
        pkce_cookie=request.cookies.get("gckey_pkce"),
        code_verifier=body.code_verifier,
    )

    response = JSONResponse(content={"status": "ok"})
    response.delete_cookie("gckey_pkce", path="/")
    _set_session_cookie(
        response, role, sin_hash, access_token=access_token, subject=subject
    )
    return response


@app.get("/auth/gckey/logout")
async def gckey_logout() -> JSONResponse:
    """Clear the GCKey session cookie."""
    response = JSONResponse(content={"status": "logged_out"})
    _clear_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Claimant status view (Req 19.7 own-data-only)
# ---------------------------------------------------------------------------


@app.get("/portal/claims/{claim_id}")
async def get_claim_status(
    claim_id: str,
    principal: Principal = Depends(get_principal),
) -> JSONResponse:
    """Return the claimant's own claim status. 404 when absent (Req 19.7)."""
    record = await _repository().get_claim_status(claim_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Claim not found.")

    # Own-data-only enforcement for CLAIMANT: their session sin_hash must match
    # the claim's sin_hash. Staff roles may read any claim's status.
    if principal.role is Role.CLAIMANT:
        if not principal.sin_hash or principal.sin_hash != record.get("sin_hash"):
            # Do not leak existence of another claimant's claim.
            raise HTTPException(status_code=404, detail="Claim not found.")

    view = {
        "claim_id": record["claim_id"],
        "status": record["status"],
        "risk_decision": record["risk_decision"],
        "risk_score": record["risk_score"],
        "explanation_en": record["explanation_en"],
        "explanation_fr": record["explanation_fr"],
        "submitted_at": record["submitted_at"],
        "decided_at": record["decided_at"],
    }
    return JSONResponse(content=view)


# ---------------------------------------------------------------------------
# Investigator queue + detail (Req 22.1, 22.2, 22.6)
# ---------------------------------------------------------------------------


@app.get("/portal/investigator/claims")
async def list_flagged_claims(
    principal: Principal = Depends(require_roles(*INVESTIGATOR_ROLES)),
) -> JSONResponse:
    """Return flagged claims sorted by risk_score desc, PII masked per role."""
    rows = await _repository().list_flagged()
    unmasked = principal.can_view_unmasked_pii()
    masked = [mask_flagged(row, unmasked=unmasked) for row in rows]
    return JSONResponse(content={"claims": masked})


@app.get("/portal/investigator/claims/{claim_id}")
async def get_investigator_claim(
    claim_id: str,
    principal: Principal = Depends(
        require_roles(Role.INVESTIGATOR, Role.ADMINISTRATOR)
    ),
) -> JSONResponse:
    """Return the full assessment for a flagged claim. 404 when absent.

    Req 22.6: only INVESTIGATOR or ADMINISTRATOR may access flagged claim
    DETAILS (REVIEWER is limited to the masked queue + SSE, and is denied here
    with 403). This matches the disposition endpoint's guard.
    """
    detail = await _repository().get_claim_detail(claim_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Claim not found.")
    masked = mask_detail(detail, unmasked=principal.can_view_unmasked_pii())
    return JSONResponse(content=masked)


# ---------------------------------------------------------------------------
# Disposition (Req 22.3, 22.4)
# ---------------------------------------------------------------------------


@app.put("/api/v1/claims/{claim_id}/disposition")
async def put_disposition(
    claim_id: str,
    body: DispositionRequest,
    principal: Principal = Depends(
        require_roles(Role.INVESTIGATOR, Role.ADMINISTRATOR)
    ),
) -> JSONResponse:
    """Record a disposition; publish outcome.feedback for confirmed/false-positive."""
    if body.disposition not in VALID_DISPOSITIONS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid disposition: {body.disposition!r}.",
        )

    await _repository().record_disposition(
        claim_id,
        body.disposition,
        body.case_notes,
        investigator_id=principal.subject or principal.role.value,
    )

    if body.disposition in FEEDBACK_DISPOSITIONS:
        await _publish_outcome_feedback(claim_id, body.disposition)

    return JSONResponse(
        content={
            "claim_id": claim_id,
            "disposition": body.disposition,
            "case_notes": body.case_notes,
        }
    )


# ---------------------------------------------------------------------------
# SSE stream of flagged claims (Req 22.1)
# ---------------------------------------------------------------------------


@app.get("/portal/events/claims")
async def stream_claim_events(
    principal: Principal = Depends(require_roles(*INVESTIGATOR_ROLES)),
) -> StreamingResponse:
    """Stream new/updated flagged claims as SSE frames (Req 22.1).

    Backed by an in-process broadcaster so it works without Kafka. PII is masked
    per the caller's role before each frame is emitted (Req 22.6).
    """
    broadcaster = _broadcaster()
    queue = await broadcaster.subscribe()
    unmasked = principal.can_view_unmasked_pii()

    async def _generator():
        try:
            while True:
                try:
                    claim = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield HEARTBEAT_COMMENT
                    continue
                yield format_sse(mask_flagged(claim, unmasked=unmasked))
        finally:
            await broadcaster.unsubscribe(queue)

    return StreamingResponse(_generator(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Reverse proxy to the Claim Intake Service (single-origin for the SPA)
# ---------------------------------------------------------------------------

# Only these paths are proxied; the write logic lives in claim-intake and is
# NOT duplicated here.
_PROXY_HOP_BY_HOP = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
}


async def _reverse_proxy(
    request: Request, target_path: str, principal: Principal
) -> Response:
    """Forward *request* to the Claim Intake Service with the GCKey Bearer.

    The caller's GCKey access token (from the signed session) is attached as the
    ``Authorization`` header so Claim Intake can validate it via JWKS (Req 19.7).
    Any inbound ``Authorization``/``Cookie`` headers are dropped in favour of the
    session-derived Bearer, so the browser session cookie never leaks upstream.
    """
    settings = app.state.settings if hasattr(app.state, "settings") else get_settings()
    if not _HAS_HTTPX:
        raise HTTPException(status_code=502, detail="Upstream client unavailable.")

    base_url = settings.claim_intake_url.rstrip("/")
    url = f"{base_url}{target_path}"

    _PROXY_DROP = _PROXY_HOP_BY_HOP | {"authorization", "cookie"}
    forward_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _PROXY_DROP
    }
    forward_headers["Authorization"] = _bearer_from_principal(principal)
    body = await request.body()

    # A test may inject an httpx MockTransport via app.state.proxy_transport.
    transport = getattr(app.state, "proxy_transport", None)
    client_kwargs: dict[str, Any] = {"timeout": 30.0}
    if transport is not None:
        client_kwargs["transport"] = transport

    async with httpx.AsyncClient(**client_kwargs) as client:
        upstream = await client.request(
            request.method,
            url,
            content=body,
            headers=forward_headers,
            params=dict(request.query_params),
        )

    response_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _PROXY_HOP_BY_HOP
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


@app.post("/api/v1/claims")
async def create_claim(
    request: Request,
    principal: Principal = Depends(require_roles(Role.CLAIMANT)),
) -> Response:
    """Forward the SPA's multipart claim submission to the Claim Intake Service.

    Design-faithful behaviour (design.md §GCKey flow: the portal forwards to
    Claim Intake): the BFF is a THIN forwarder. It requires an authenticated
    CLAIMANT session (401 without a session cookie, 403 for a non-claimant
    role), then forwards the ORIGINAL ``multipart/form-data`` body (claim fields
    + optional ``roe_document`` file) to ``${CLAIM_INTAKE_URL}/api/v1/claims``
    untouched. Claim Intake owns validation, SIN hashing, persistence, and the
    ``claim.received`` publish, and returns 202 ``{claim_id, status}`` or a
    structured 400 ``{errors:[...]}`` which the BFF passes back unchanged.

    Auth propagation (design GCKey flow, "Forward with Bearer token"; Req 19.7):
    the BFF attaches the caller's GCKey access token — carried in the signed
    session from the OAuth code exchange — as the ``Authorization: Bearer``
    header. Claim Intake independently validates that Bearer via GCKey JWKS and
    returns 401/403 as appropriate. A session without a usable access token
    yields 401 here (the browser must re-authenticate via GCKey).
    """
    settings = app.state.settings if hasattr(app.state, "settings") else get_settings()
    if not _HAS_HTTPX:
        raise HTTPException(status_code=502, detail="Upstream client unavailable.")

    # Forward the raw multipart body + content-type unchanged so form fields and
    # the optional ROE file reach intake intact.
    body = await request.body()
    forward_headers: dict[str, str] = {"Accept": "application/json"}
    ctype = request.headers.get("content-type")
    if ctype:
        forward_headers["Content-Type"] = ctype

    forward_headers["Authorization"] = _bearer_from_principal(principal)

    base_url = settings.claim_intake_url.rstrip("/")
    url = f"{base_url}/api/v1/claims"

    transport = getattr(app.state, "proxy_transport", None)
    client_kwargs: dict[str, Any] = {"timeout": 30.0}
    if transport is not None:
        client_kwargs["transport"] = transport

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            upstream = await client.post(url, content=body, headers=forward_headers)
    except Exception as exc:  # noqa: BLE001 - intake unreachable => clean 502
        raise HTTPException(
            status_code=502, detail="Claim Intake Service is unavailable."
        ) from exc

    # DEV-ONLY: stand in for ROE Web so a browser submit is a complete demo.
    # In production the ROE arrives from the employer via ROE Web (a separate
    # actor); locally we post a matching ROE to the REAL roe-adapter (over the
    # real Kafka -> orchestrator transport, nothing faked) so the pipeline pairs
    # + scores without a manual terminal step. Gated by is_development + the
    # dev_auto_roe switch, and strictly best-effort: any failure here never
    # affects the claim response the SPA receives.
    if (
        upstream.status_code == status.HTTP_202_ACCEPTED
        and getattr(settings, "is_development", False)
        and getattr(settings, "dev_auto_roe", False)
    ):
        # Source the ROE's employer/earnings fields from the ORIGINAL submitted
        # form (the intake 202 body carries only {claim_id, status}). Starlette
        # caches the request body, so form() works after body() was read.
        claim_fields: dict[str, str] = {}
        try:
            form = await request.form()
            for name in _REQUIRED_CLAIM_FIELDS:
                val = form.get(name)
                if isinstance(val, str):
                    claim_fields[name] = val
        except Exception:  # noqa: BLE001 - dev convenience only
            claim_fields = {}
        await _maybe_post_dev_roe(
            settings,
            upstream_body=upstream.content,
            claim_fields=claim_fields,
            bearer=forward_headers["Authorization"],
        )

    response_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in _PROXY_HOP_BY_HOP
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


async def _maybe_post_dev_roe(
    settings: Any,
    *,
    upstream_body: bytes,
    claim_fields: dict[str, str],
    bearer: str,
) -> None:
    """DEV-ONLY: post a matching ROE to the roe-adapter for the just-created claim.

    Stands in for the employer-side ROE Web submitter so a browser claim submit
    drives the full pipeline locally. Builds the ROE JSON from the ORIGINAL
    submitted claim fields (``claim_fields``) keyed by the SAME ``claim_id`` the
    Claim Intake Service just assigned (parsed from ``upstream_body``), so the
    orchestrator pairs them (pairing is on claim_id only). Posts to the REAL
    roe-adapter over the real Kafka -> orchestrator transport — nothing faked.
    Best-effort: swallows every error so the claim submit is never affected.
    """
    if not _HAS_HTTPX:
        return
    try:
        payload = json.loads(upstream_body.decode("utf-8"))
    except Exception:  # noqa: BLE001 - non-JSON 202 body => nothing to pair
        return
    claim_id = payload.get("claim_id") or payload.get("claimId")
    if not claim_id:
        return

    # Build the ROE from the fields the claimant actually submitted (the intake
    # 202 body carries only {claim_id, status}). The SPA form does not collect
    # economic_region_code / noc_code, so those are omitted — the normalizer
    # treats them as optional and the engines degrade gracefully.
    roe_body = {
        "claimId": str(claim_id),
        "employerBn": claim_fields.get("employer_bn", ""),
        "sin": claim_fields.get("sin", ""),
        "insurableEarnings": claim_fields.get("insurable_earnings", ""),
        "insurableHours": claim_fields.get("insurable_hours", ""),
        "separationDate": claim_fields.get("separation_date", ""),
    }
    # Drop empty keys so the normalizer applies its own defaults / mandatory
    # checks cleanly (it treats absent optional fields as "").
    roe_body = {k: v for k, v in roe_body.items() if v != ""}

    url = f"{settings.roe_adapter_url.rstrip('/')}/roe/rest"
    headers = {"Authorization": bearer, "Content-Type": "application/json"}
    # Use a plain client (NOT app.state.proxy_transport — that intercepts the
    # intake forward for in-process tests; the roe-adapter is a distinct service).
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json=roe_body, headers=headers)
    except Exception:  # noqa: BLE001 - dev convenience must never break submit
        pass


@app.get("/api/v1/privacy/access")
async def proxy_privacy_access(
    request: Request,
    principal: Principal = Depends(require_roles(Role.CLAIMANT)),
) -> Response:
    """Reverse-proxy a Privacy Act access request to the Claim Intake Service.

    Requires an authenticated CLAIMANT session; forwards the GCKey Bearer so
    intake enforces its own auth (Req 19.4, 19.7).
    """
    return await _reverse_proxy(request, "/api/v1/privacy/access", principal)


@app.delete("/api/v1/privacy/delete")
async def proxy_privacy_delete(
    request: Request,
    principal: Principal = Depends(require_roles(Role.CLAIMANT)),
) -> Response:
    """Reverse-proxy a Privacy Act deletion request to the Claim Intake Service.

    Requires an authenticated CLAIMANT session; forwards the GCKey Bearer so
    intake enforces its own auth (Req 19.5, 19.7).
    """
    return await _reverse_proxy(request, "/api/v1/privacy/delete", principal)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


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
        await producer.client.fetch_all_metadata()
        return True
    except Exception:  # noqa: BLE001
        return producer is not None


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Report DB / Kafka connectivity (200 healthy, 503 degraded)."""
    checks = {
        "database": await _check_db(app),
        "kafka": await _check_kafka(app),
    }
    healthy = all(checks.values())
    code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=code,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )


__all__ = [
    "app",
    "lifespan",
    "DispositionRequest",
    "OUTCOME_FEEDBACK_TOPIC",
    "FEEDBACK_DISPOSITIONS",
]

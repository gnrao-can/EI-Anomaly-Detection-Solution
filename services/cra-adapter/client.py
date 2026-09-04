"""
services/cra-adapter/client.py
==============================
CRA T4 data-exchange client (task 19.1).

This module owns everything about *fetching* a T4 record from the Canada Revenue
Agency and normalising it into the minimal, data-minimised shape the rest of
EI-ADS is allowed to see:

  - :func:`build_mtls_client` — constructs an ``httpx.Client`` configured for
    **mTLS 1.3** using the client certificate/key and trusted CA bundle from
    settings (``cert=(certfile, keyfile)``, ``verify=ca_bundle``). Documented and
    wired for production; in dev/test no real mTLS handshake is performed and a
    synthetic generator is used instead.
  - :data:`ALLOWED_FIELDS` and :func:`project_allowed_fields` — the
    data-minimisation filter applied to *every* response so only permitted
    fields ever leave the adapter (Requirement 11.2).
  - :func:`discard_if_too_old` / :func:`is_record_too_old` — the ">10 years old"
    retention rule (Requirement 11.7): stale records are dropped and a warning is
    logged.
  - :func:`generate_synthetic_t4` — deterministic synthetic T4 records with the
    same field structure as production, for dev/test (Requirement 11.8).
  - :class:`CRAClient` — ties it together: fetch (real or synthetic), apply the
    age rule, then project to the allowed fields.

The T4 "record" is a plain ``dict``. A production upstream response may carry
extra fields (address, employer legal name, etc.); the projection guarantees
those never propagate.

Requirements: 11.1, 11.2, 11.7, 11.8
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("cra-adapter.client")


# ---------------------------------------------------------------------------
# Data minimisation (Requirement 11.2)
# ---------------------------------------------------------------------------

#: The only fields permitted to leave the CRA Adapter. Every response — real or
#: synthetic — is filtered down to (at most) these keys before it is returned.
ALLOWED_FIELDS: frozenset[str] = frozenset(
    {"sin", "tax_year", "employer_bn", "employment_income", "noc_category"}
)


def project_allowed_fields(record: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *record* containing only :data:`ALLOWED_FIELDS`.

    This is the data-minimisation filter (Requirement 11.2): any field not in
    the allow-list (e.g. address, employer legal name, phone) is stripped so it
    can never propagate downstream. Missing allowed fields are simply absent
    from the result (they are not fabricated).
    """
    return {k: v for k, v in record.items() if k in ALLOWED_FIELDS}


# ---------------------------------------------------------------------------
# Retention rule (Requirement 11.7)
# ---------------------------------------------------------------------------


def is_record_too_old(
    tax_year: int, *, max_age_years: int = 10, reference: Optional[date] = None
) -> bool:
    """Return ``True`` when a T4 for *tax_year* is older than *max_age_years*.

    Age is measured in whole years between the record's tax year and the
    reference year (defaults to the current UTC year). A record from exactly
    ``max_age_years`` ago is retained; one older than that is too old.
    """
    ref_year = (reference or datetime.now(timezone.utc).date()).year
    return (ref_year - int(tax_year)) > max_age_years


def discard_if_too_old(
    record: Optional[dict[str, Any]],
    *,
    max_age_years: int = 10,
    reference: Optional[date] = None,
) -> Optional[dict[str, Any]]:
    """Return *record* unless its ``tax_year`` is > *max_age_years* old.

    When the record is too old it is discarded (``None`` is returned) and a
    warning is logged (Requirement 11.7). No SIN is included in the log line.
    """
    if record is None:
        return None
    tax_year = record.get("tax_year")
    if tax_year is None:
        return record
    if is_record_too_old(int(tax_year), max_age_years=max_age_years, reference=reference):
        logger.warning(
            "Discarding T4 record for tax_year=%s: older than %d-year retention "
            "limit (employer_bn=%s)",
            tax_year,
            max_age_years,
            record.get("employer_bn", "?"),
        )
        return None
    return record


# ---------------------------------------------------------------------------
# Synthetic T4 generation for dev/test (Requirement 11.8)
# ---------------------------------------------------------------------------


def _deterministic_int(seed: str, low: int, high: int) -> int:
    """Deterministically map a seed string to an int in ``[low, high]``."""
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    span = high - low + 1
    return low + (int(digest[:12], 16) % span)


def generate_synthetic_t4(
    sin_hash: str, tax_year: int, employer_bn: str
) -> dict[str, Any]:
    """Generate a deterministic synthetic T4 record for dev/test.

    The record has the **same field structure** as a production upstream
    response, including some extra (non-allowed) fields so that tests exercise
    the data-minimisation projection. Values are deterministic in the inputs so
    tests are reproducible. The raw SIN is never fabricated — only ``sin_hash``
    is echoed into the ``sin`` field (kept opaque; downstream never receives a
    real SIN).

    Requirement 11.8: dev/test synthetic records mirror the production shape.
    """
    income = _deterministic_int(f"{sin_hash}:{tax_year}:{employer_bn}", 15_000, 95_000)
    noc_categories = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"]
    noc = noc_categories[_deterministic_int(f"noc:{employer_bn}", 0, 9)]
    return {
        # ── allowed / minimised fields ──
        "sin": sin_hash,  # opaque; raw SIN never used
        "tax_year": int(tax_year),
        "employer_bn": employer_bn,
        "employment_income": float(income),
        "noc_category": noc,
        # ── extra production-shaped fields that MUST be stripped ──
        "employer_legal_name": f"Synthetic Employer {employer_bn[:4]}",
        "employer_address": "123 Test St, Ottawa, ON",
        "cpp_contributions": round(income * 0.0595, 2),
        "ei_premiums": round(income * 0.0166, 2),
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# mTLS httpx client construction (Requirement 11.1)
# ---------------------------------------------------------------------------


def build_mtls_client(settings: Any) -> Any:
    """Build an ``httpx.Client`` configured for mTLS 1.3 from *settings*.

    Production: the client presents ``cert=(cra_client_cert_path,
    cra_client_key_path)`` and verifies the server against
    ``verify=cra_ca_cert_path`` (the trusted GC CA bundle), negotiating TLS 1.3.
    ``base_url`` and ``timeout`` come from settings.

    Returns ``None`` when ``httpx`` is unavailable or no mTLS material is
    configured (local dev), signalling callers to use the synthetic generator
    instead. This keeps the module importable and unit-testable without network
    access or certificate files.
    """
    try:
        import httpx  # imported lazily so the module loads without httpx
    except ImportError:  # pragma: no cover - httpx should be a pinned dep
        logger.warning("httpx unavailable; CRA client will use synthetic data")
        return None

    if not getattr(settings, "has_mtls_material", False):
        logger.info(
            "No mTLS client certificate configured; CRA client will use "
            "synthetic T4 data (dev/test mode)"
        )
        return None

    cert = (settings.cra_client_cert_path, settings.cra_client_key_path)
    verify = settings.cra_ca_cert_path or True

    # httpx negotiates the highest mutually supported TLS version; the GC secure
    # exchange requires TLS 1.3. We build an SSL context pinned to 1.3 when the
    # platform's OpenSSL supports it.
    try:
        import ssl

        ssl_context = ssl.create_default_context(cafile=settings.cra_ca_cert_path or None)
        if hasattr(ssl, "TLSVersion"):
            ssl_context.minimum_version = ssl.TLSVersion.TLSv1_3
        ssl_context.load_cert_chain(
            certfile=settings.cra_client_cert_path,
            keyfile=settings.cra_client_key_path,
        )
        return httpx.Client(
            base_url=settings.cra_base_url,
            verify=ssl_context,
            timeout=settings.cra_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - fall back to cert tuple form
        logger.warning(
            "Falling back to cert-tuple mTLS config (context build failed): %s", exc
        )
        return httpx.Client(
            base_url=settings.cra_base_url,
            cert=cert,
            verify=verify,
            timeout=settings.cra_timeout_seconds,
        )


# ---------------------------------------------------------------------------
# CRAClient
# ---------------------------------------------------------------------------


@dataclass
class T4Result:
    """Outcome of a T4 lookup.

    Attributes
    ----------
    status:
        ``"OK"`` when a minimised record is returned, ``"NOT_FOUND"`` when the
        record was absent or discarded by the retention rule.
    record:
        The minimised T4 record (only :data:`ALLOWED_FIELDS`) when ``status``
        is ``"OK"``; otherwise ``None``.
    """

    status: str
    record: Optional[dict[str, Any]] = None


class CRAClient:
    """Fetches and minimises T4 records.

    In dev/test (no mTLS material) the client synthesises records. In production
    it issues an mTLS GET to the CRA secure exchange. Either way, the record is
    run through the >10-year retention rule and then the data-minimisation
    projection before being returned, so those invariants hold on *every* path.
    """

    def __init__(self, settings: Any, http_client: Any = None) -> None:
        self._settings = settings
        # An injected client (real or fake) takes precedence; otherwise build
        # one from settings (which returns None in dev, triggering synthetic).
        self._http = http_client if http_client is not None else build_mtls_client(settings)

    @property
    def uses_synthetic_data(self) -> bool:
        """True when this client will synthesise T4 records (no live upstream)."""
        return self._http is None

    def _fetch_raw(
        self, sin_hash: str, tax_year: int, employer_bn: str
    ) -> Optional[dict[str, Any]]:
        """Fetch the raw (un-minimised) T4 record, real or synthetic."""
        if self._http is None:
            return generate_synthetic_t4(sin_hash, tax_year, employer_bn)

        path = f"/cra/t4/{sin_hash}/{tax_year}/{employer_bn}"
        response = self._http.get(path)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def get_t4(self, sin_hash: str, tax_year: int, employer_bn: str) -> T4Result:
        """Return the minimised T4 for the given key, applying all invariants.

        Steps (each applied on every path):
          1. Fetch the raw record (mTLS upstream in prod; synthetic in dev/test).
          2. Discard records older than the retention limit (logs a warning).
          3. Project to :data:`ALLOWED_FIELDS` (data minimisation).
        """
        raw = self._fetch_raw(sin_hash, tax_year, employer_bn)
        if raw is None:
            return T4Result(status="NOT_FOUND")

        kept = discard_if_too_old(
            raw, max_age_years=self._settings.cra_max_record_age_years
        )
        if kept is None:
            return T4Result(status="NOT_FOUND")

        return T4Result(status="OK", record=project_allowed_fields(kept))

    def close(self) -> None:
        """Close the underlying HTTP client if one is held."""
        if self._http is not None:
            try:
                self._http.close()
            except Exception:  # noqa: BLE001
                pass


__all__ = [
    "ALLOWED_FIELDS",
    "project_allowed_fields",
    "is_record_too_old",
    "discard_if_too_old",
    "generate_synthetic_t4",
    "build_mtls_client",
    "T4Result",
    "CRAClient",
]

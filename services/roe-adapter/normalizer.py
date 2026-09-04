"""
services/roe-adapter/normalizer.py
==================================
Normalisation utilities for the ROE Adapter.

Two responsibilities:

1. ``normalize_roe(raw: dict) -> Claim`` — maps the many variant ROE field
   names (as produced by ROE Web REST/JSON, REST/XML, and SOAP payloads) onto
   the canonical :class:`shared.models.Claim` model.

2. ``detect_and_decode(raw_bytes: bytes) -> str`` — inspects a raw request body
   and decodes it as UTF-8 when possible, otherwise ISO-8859-1 (Latin-1), which
   is the legacy encoding still emitted by some ROE Web endpoints.

Field-name normalisation is intentionally forgiving: ROE Web, its SOAP WSDL, and
hand-rolled XML all use slightly different names/casing for the same concept
(e.g. ``insurableHours`` / ``insurable_hours`` / ``total_insurable_hours``). All
known aliases map to a single canonical field.

Requirements: 1.6, 1.7, 1.8
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from shared.models import Claim, ClaimStatus
from shared.validation import normalize_bn, normalize_sin


class NormalizationError(ValueError):
    """Raised when a raw ROE payload cannot be normalised to a Claim.

    Carries a structured ``errors`` list so the API layer can return a
    machine-readable error response (HTTP 422) without publishing to Kafka.
    """

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__("; ".join(e.get("message", "") for e in errors))


# ---------------------------------------------------------------------------
# Field alias mapping
# ---------------------------------------------------------------------------
#
# Each canonical field maps to an ordered tuple of accepted ROE aliases. Lookup
# is case-insensitive and ignores separators (``_``, ``-``, spaces) — aliases
# below are written in a normalised (lowercase, separator-free) form.

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "sin": ("sin", "socialinsurancenumber", "socialinsuranceno", "employeesin"),
    "employer_bn": (
        "employerbn",
        "bn",
        "businessnumber",
        "employerbusinessnumber",
        "cra bn",
        "crabn",
    ),
    "insurable_hours": (
        "insurablehours",
        "totalinsurablehours",
        "hours",
        "block15a",  # ROE Block 15A = total insurable hours
        "totalhours",
    ),
    "insurable_earnings": (
        "insurableearnings",
        "totalinsurableearnings",
        "earnings",
        "block15b",  # ROE Block 15B = total insurable earnings
        "totalearnings",
    ),
    "separation_date": (
        "separationdate",
        "lastdayworked",
        "lastdaypaid",
        "finalpayperiodenddate",
        "block11",  # ROE Block 11 = last day for which paid
        "dateofseparation",
    ),
    "separation_reason": (
        "separationreason",
        "reasonforseparation",
        "reason",
        "commentsreason",
        "block18",  # ROE Block 18 = comments
    ),
    "separation_reason_code": (
        "separationreasoncode",
        "reasoncode",
        "code",
        "block16",  # ROE Block 16 = reason for issuing this ROE
        "roecode",
    ),
    "claim_start_date": (
        "claimstartdate",
        "firstdayworked",
        "periodofemploymentstart",
        "block10",  # ROE Block 10 = first day worked
        "benefitperiodstart",
    ),
    "economic_region_code": (
        "economicregioncode",
        "economicregion",
        "region",
        "esdcregion",
        "esdcregioncode",
    ),
    "noc_code": ("noccode", "noc", "occupationcode", "nationaloccupationcode"),
    "residential_postal_code": (
        "residentialpostalcode",
        "postalcode",
        "employeepostalcode",
        "postal",
    ),
    "residential_address": (
        "residentialaddress",
        "employeeaddress",
        "address",
        "homeaddress",
    ),
    "residential_address_hash": ("residentialaddresshash", "addresshash"),
    "employer_work_location": (
        "employerworklocation",
        "worklocation",
        "employeraddress",
        "employeraddressline",
        "block8",  # ROE Block 8 = employer address
    ),
    "roe_document_id": ("roedocumentid", "documentid", "roeid", "roeserialnumber"),
    "claim_id": ("claimid", "claimidentifier"),
}


def _normalise_key(key: str) -> str:
    """Lowercase a key and strip separators so aliases match loosely."""
    return re.sub(r"[\s_\-]+", "", key).lower()


def _build_lookup(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a dict keyed by normalised field name -> value.

    Later keys with the same normalised form do not overwrite earlier ones.
    """
    lookup: dict[str, Any] = {}
    for key, value in raw.items():
        norm = _normalise_key(str(key))
        if norm not in lookup:
            lookup[norm] = value
    return lookup


def _get(lookup: dict[str, Any], canonical: str) -> Any:
    """Fetch the value for a canonical field from the alias lookup, or ``None``."""
    for alias in _FIELD_ALIASES.get(canonical, ()):  # aliases already normalised
        if alias in lookup and lookup[alias] not in (None, ""):
            return lookup[alias]
    return None


# ---------------------------------------------------------------------------
# Value parsers
# ---------------------------------------------------------------------------


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_decimal(value: Any) -> Decimal:
    """Parse a monetary/hours value to Decimal via the string path."""
    if isinstance(value, Decimal):
        return value
    text = str(value).strip().replace(",", "").replace("$", "")
    return Decimal(text)


def _parse_date(value: Any) -> date:
    """Parse a date from ISO ``YYYY-MM-DD`` or common ROE variants."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    # ISO first (handles YYYY-MM-DD and full timestamps)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date format: {value!r}")


# ---------------------------------------------------------------------------
# Encoding detection
# ---------------------------------------------------------------------------


def detect_and_decode(raw_bytes: bytes) -> str:
    """Decode *raw_bytes* using UTF-8 when valid, else ISO-8859-1 (Latin-1).

    ROE Web historically emits ISO-8859-1 (Latin-1) payloads while newer
    endpoints use UTF-8. UTF-8 is strictly validated first; on any decode error
    the bytes are decoded as ISO-8859-1, which cannot fail for any byte sequence.

    Returns the decoded ``str``. Accepts and strips a UTF-8 BOM if present.
    """
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise TypeError("detect_and_decode expects bytes")

    # Honour an explicit UTF-8 BOM.
    if raw_bytes[:3] == b"\xef\xbb\xbf":
        return raw_bytes[3:].decode("utf-8")

    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # Latin-1 maps every byte 0x00-0xFF to a code point; never raises.
        return raw_bytes.decode("iso-8859-1")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def normalize_roe(raw: dict[str, Any]) -> Claim:
    """Map a raw ROE field dict onto the canonical :class:`Claim` model.

    The input may use any of the known ROE field-name aliases (see
    ``_FIELD_ALIASES``) with arbitrary casing/separators. Missing or malformed
    mandatory fields raise :class:`NormalizationError` carrying a structured
    error list; the caller must NOT publish to Kafka in that case.
    """
    if not isinstance(raw, dict):
        raise NormalizationError(
            [{"field": "_root", "code": "INVALID", "message": "ROE payload must be an object."}]
        )

    lookup = _build_lookup(raw)
    errors: list[dict[str, str]] = []

    # --- SIN -> sin_hash (raw SIN never stored) ---
    sin_hash: str = ""
    sin_value = _get(lookup, "sin")
    if sin_value is None:
        errors.append({"field": "sin", "code": "MISSING", "message": "SIN is required."})
    else:
        try:
            sin_hash = _sha256(normalize_sin(str(sin_value)))
        except ValueError as exc:
            errors.append({"field": "sin", "code": "INVALID", "message": str(exc)})

    # --- Employer BN ---
    employer_bn = ""
    bn_value = _get(lookup, "employer_bn")
    if bn_value is None:
        errors.append(
            {"field": "employer_bn", "code": "MISSING", "message": "Employer BN is required."}
        )
    else:
        try:
            employer_bn = normalize_bn(str(bn_value))
        except ValueError as exc:
            errors.append({"field": "employer_bn", "code": "INVALID", "message": str(exc)})

    # --- Insurable hours ---
    insurable_hours = Decimal("0")
    hours_value = _get(lookup, "insurable_hours")
    if hours_value is None:
        errors.append(
            {"field": "insurable_hours", "code": "MISSING", "message": "Insurable hours is required."}
        )
    else:
        try:
            insurable_hours = _parse_decimal(hours_value)
            if insurable_hours <= 0:
                errors.append(
                    {
                        "field": "insurable_hours",
                        "code": "INVALID",
                        "message": "Insurable hours must be greater than 0.",
                    }
                )
        except (InvalidOperation, ValueError):
            errors.append(
                {
                    "field": "insurable_hours",
                    "code": "INVALID",
                    "message": "Insurable hours must be numeric.",
                }
            )

    # --- Insurable earnings ---
    insurable_earnings = Decimal("0")
    earnings_value = _get(lookup, "insurable_earnings")
    if earnings_value is None:
        errors.append(
            {
                "field": "insurable_earnings",
                "code": "MISSING",
                "message": "Insurable earnings is required.",
            }
        )
    else:
        try:
            insurable_earnings = _parse_decimal(earnings_value)
            if insurable_earnings <= 0:
                errors.append(
                    {
                        "field": "insurable_earnings",
                        "code": "INVALID",
                        "message": "Insurable earnings must be greater than 0.",
                    }
                )
        except (InvalidOperation, ValueError):
            errors.append(
                {
                    "field": "insurable_earnings",
                    "code": "INVALID",
                    "message": "Insurable earnings must be numeric.",
                }
            )

    # --- Separation date ---
    separation_date: date | None = None
    sep_value = _get(lookup, "separation_date")
    if sep_value is None:
        errors.append(
            {"field": "separation_date", "code": "MISSING", "message": "Separation date is required."}
        )
    else:
        try:
            separation_date = _parse_date(sep_value)
        except ValueError:
            errors.append(
                {
                    "field": "separation_date",
                    "code": "INVALID",
                    "message": "Separation date must be a valid date.",
                }
            )

    # --- Claim start date (defaults to separation date when absent) ---
    claim_start_date: date | None = None
    start_value = _get(lookup, "claim_start_date")
    if start_value is not None:
        try:
            claim_start_date = _parse_date(start_value)
        except ValueError:
            errors.append(
                {
                    "field": "claim_start_date",
                    "code": "INVALID",
                    "message": "Claim start date must be a valid date.",
                }
            )
    if claim_start_date is None and separation_date is not None:
        claim_start_date = separation_date

    # Fail fast if any mandatory field failed.
    if errors:
        raise NormalizationError(errors)

    # --- Optional / defaulted string fields ---
    separation_reason = str(_get(lookup, "separation_reason") or "")[:1000]
    separation_reason_code = str(_get(lookup, "separation_reason_code") or "")
    economic_region_code = str(_get(lookup, "economic_region_code") or "")
    noc_code = str(_get(lookup, "noc_code") or "")

    postal_raw = _get(lookup, "residential_postal_code")
    residential_postal_code = ""
    if postal_raw is not None:
        # Retain only the FSA (first 3 chars) per Protected B data minimisation.
        residential_postal_code = str(postal_raw).replace(" ", "").upper()[:3]

    # Address: prefer an explicit hash, else hash a supplied full address.
    address_hash = _get(lookup, "residential_address_hash")
    if address_hash is None:
        address_value = _get(lookup, "residential_address")
        residential_address_hash = _sha256(str(address_value)) if address_value else ""
    else:
        residential_address_hash = str(address_hash)

    employer_work_location = str(_get(lookup, "employer_work_location") or "")

    # --- Identifiers ---
    claim_id_value = _get(lookup, "claim_id")
    try:
        claim_id = uuid.UUID(str(claim_id_value)) if claim_id_value else uuid.uuid4()
    except (ValueError, AttributeError, TypeError):
        claim_id = uuid.uuid4()

    roe_doc_value = _get(lookup, "roe_document_id")
    roe_document_id: uuid.UUID | None
    try:
        roe_document_id = uuid.UUID(str(roe_doc_value)) if roe_doc_value else None
    except (ValueError, AttributeError, TypeError):
        roe_document_id = None

    return Claim(
        claim_id=claim_id,
        sin_hash=sin_hash,
        employer_bn=employer_bn,
        insurable_hours=insurable_hours,
        insurable_earnings=insurable_earnings,
        separation_date=separation_date,  # type: ignore[arg-type]
        separation_reason=separation_reason,
        separation_reason_code=separation_reason_code,
        claim_start_date=claim_start_date,  # type: ignore[arg-type]
        economic_region_code=economic_region_code,
        noc_code=noc_code,
        residential_postal_code=residential_postal_code,
        residential_address_hash=residential_address_hash,
        employer_work_location=employer_work_location,
        received_at=datetime.now(timezone.utc),
        status=ClaimStatus.RECEIVED,
        roe_document_id=roe_document_id,
    )


# ---------------------------------------------------------------------------
# XML parsing helpers
# ---------------------------------------------------------------------------


def xml_to_dict(xml_text: str) -> dict[str, Any]:
    """Flatten a simple ROE XML document into a ``{tag: text}`` dict.

    Uses ``lxml`` when available and falls back to the stdlib ``ElementTree``.
    Namespaces are stripped from tag names so that both namespaced (SOAP) and
    plain XML resolve to the same field names. Leaf text values are collected;
    repeated leaf tags keep the first occurrence.
    """
    try:
        from lxml import etree  # type: ignore

        root = etree.fromstring(xml_text.encode("utf-8"))
    except ImportError:  # pragma: no cover - stdlib fallback
        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml_text)

    result: dict[str, Any] = {}

    def _local(tag: Any) -> str:
        tag = str(tag)
        return tag.split("}", 1)[1] if "}" in tag else tag

    def _walk(node: Any) -> None:
        children = list(node)
        if not children:
            text = (node.text or "").strip()
            if text:
                key = _local(node.tag)
                if key not in result:
                    result[key] = text
            return
        for child in children:
            _walk(child)

    _walk(root)
    return result

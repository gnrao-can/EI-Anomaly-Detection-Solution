"""
services/document-forgery-engine/signature_verification.py
==========================================================
Digital signature verification for the Document Forgery Engine (task 15.3).

Design step 5 of the Document Forgery Engine (design "5. Document Forgery
Engine") requires:

  5. Digital signature verification:
     - Extract embedded PKCS#7 / CAdES signature
     - Verify against employer's registered X.509 certificate hash
     - If no signature present -> CRITICAL Indicator (Requirement 7.6)
     - If hash mismatch       -> CRITICAL Indicator (Requirement 7.5)

Extraction / certificate parsing uses the ``cryptography`` library (pinned
``44.0.0``). A PKCS#7 / CAdES signature is a DER- (or PEM-) encoded
``SignedData`` structure carrying one or more signer X.509 certificates. We
load the signature, take the signer certificate, compute its SHA-256 fingerprint
(hex, lowercase, no separators) and compare it to the employer's registered
certificate hash from the EI-ADS employer signature registry (Requirement 7.5).

Sandbox / dependency fallback
------------------------------
The multi-platform ``cryptography`` wheel may not be importable in every
sandbox. The dependency is therefore isolated behind
:func:`extract_signature_certificate_hash`, which:

  * uses ``cryptography.hazmat.primitives.serialization.pkcs7`` to load the
    PKCS#7 ``SignedData`` and read the signer certificate when the library is
    importable, and
  * otherwise returns ``SignatureExtraction(available=False, ...)`` so the
    caller can fall back gracefully.

For full testability without ``cryptography``, callers may also supply an
already-computed certificate hash directly (``document["signature_cert_hash"]``)
or the raw signature bytes (``document["signature"]`` / ``signature_pkcs7``).
The forensic decision logic in :func:`check_digital_signature` is pure and
depends only on the extracted hash + the registered hash, so it is fully
unit-testable with a fake extractor / injected hashes.

Requirements: 7.5, 7.6
"""

from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Union

from shared.models import EngineType, Indicator, Severity

logger = logging.getLogger("document-forgery-engine.signature")

#: Reason codes surfaced in the Indicator payload.
REASON_NO_SIGNATURE = "SIGNATURE_ABSENT"
REASON_HASH_MISMATCH = "SIGNATURE_HASH_MISMATCH"


@dataclass(frozen=True)
class SignatureExtraction:
    """Result of extracting the signer certificate hash from a PKCS#7 signature.

    Attributes
    ----------
    present:
        ``True`` when a signature was found embedded in the document.
    cert_hash:
        The SHA-256 fingerprint (lowercase hex, no separators) of the signer
        X.509 certificate when a signature was extracted; ``None`` otherwise.
    available:
        ``True`` when signature extraction could actually be attempted. This is
        ``False`` when the ``cryptography`` library is unavailable AND no
        pre-extracted hash/signature was supplied, so the caller can decide to
        skip the check rather than falsely flag an absent signature.
    """

    present: bool
    cert_hash: Optional[str] = None
    available: bool = True


def _normalise_hash(value: Any) -> Optional[str]:
    """Normalise a certificate hash to lowercase hex without separators."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    # Strip a leading algorithm tag (``sha256:ab:cd``) BEFORE removing the
    # separators that also appear inside colon-delimited fingerprints.
    if text.startswith("sha256:"):
        text = text[len("sha256:"):]
    # Strip common separators (``ab:cd:ef`` / ``ab cd ef`` / ``ab-cd-ef``).
    for sep in (":", " ", "-"):
        text = text.replace(sep, "")
    return text or None


def extract_signature_certificate_hash(
    document: dict[str, Any],
) -> SignatureExtraction:
    """Extract the signer certificate SHA-256 hash from an ROE's PKCS#7 signature.

    Resolution order (first hit wins):

      1. ``document["signature_cert_hash"]`` — a pre-computed signer certificate
         hash (used in tests and when an upstream step already parsed it).
      2. ``document["signature_present"]`` explicitly ``False`` -> report an
         absent signature (``present=False``) so the caller flags it.
      3. Raw PKCS#7 signature bytes under ``document["signature"]`` /
         ``document["signature_pkcs7"]`` -> parse with ``cryptography`` and
         compute the signer certificate SHA-256 fingerprint.

    Returns a :class:`SignatureExtraction`. When no signature material is present
    at all, returns ``present=False``. When signature bytes were supplied but
    ``cryptography`` is unavailable, returns ``available=False`` so the caller
    can skip rather than mis-flag.
    """
    # 1. Pre-computed hash supplied directly.
    pre_hash = _normalise_hash(document.get("signature_cert_hash"))
    if pre_hash is not None:
        return SignatureExtraction(present=True, cert_hash=pre_hash, available=True)

    # 2. Explicit "no signature" marker.
    if document.get("signature_present") is False:
        return SignatureExtraction(present=False, cert_hash=None, available=True)

    # 3. Raw signature bytes -> parse via cryptography.
    raw = _resolve_signature_bytes(document)
    if raw is None:
        # No signature material of any kind was supplied.
        return SignatureExtraction(present=False, cert_hash=None, available=True)

    cert_hash = _extract_hash_via_cryptography(raw)
    if cert_hash is None:
        # We *have* signature bytes but could not parse them. This is either a
        # missing cryptography lib (skip) or a malformed/empty signature. We
        # distinguish by probing importability.
        if not _cryptography_available():
            logger.info("cryptography unavailable; skipping signature verification")
            return SignatureExtraction(present=True, cert_hash=None, available=False)
        # cryptography present but no signer cert -> treat as absent signature.
        return SignatureExtraction(present=False, cert_hash=None, available=True)

    return SignatureExtraction(present=True, cert_hash=cert_hash, available=True)


def check_digital_signature(
    extraction: SignatureExtraction,
    registered_cert_hash: Optional[str],
    *,
    claim_id: Optional[str] = None,
) -> list[Indicator]:
    """Flag an absent or mismatched ROE digital signature.

    Produces exactly one CRITICAL Indicator when:
      * the document contains no embedded signature (``present=False``)
        -> Requirement 7.6 (absent signature), or
      * a signer certificate hash was extracted but it does not match the
        employer's ``registered_cert_hash`` -> Requirement 7.5 (hash mismatch).

    Returns ``[]`` (no Indicator) when:
      * the extracted hash matches the registered hash (authentic signature), or
      * signature verification could not be attempted (``available=False``,
        e.g. the ``cryptography`` library is missing in the sandbox) -> the
        engine simply skips this check rather than false-flagging.
    """
    if not extraction.available:
        return []

    # Absent signature (Requirement 7.6).
    if not extraction.present:
        return [
            _indicator(
                claim_id,
                reason=REASON_NO_SIGNATURE,
                title="ROE contains no embedded digital signature",
                explanation_en=(
                    "The submitted ROE document contains no embedded PKCS#7 / "
                    "CAdES digital signature, so its authenticity cannot be "
                    "verified against the employer signature registry."
                ),
                explanation_fr=(
                    "Le releve d'emploi soumis ne contient aucune signature "
                    "numerique PKCS#7 / CAdES integree; son authenticite ne "
                    "peut donc pas etre verifiee dans le registre des "
                    "signatures d'employeur."
                ),
                payload={
                    "flag": REASON_NO_SIGNATURE,
                    "signature_present": False,
                },
            )
        ]

    registered = _normalise_hash(registered_cert_hash)
    extracted = _normalise_hash(extraction.cert_hash)

    # No registered hash to compare against, or no extracted hash -> cannot
    # assert a mismatch; skip rather than false-flag.
    if registered is None or extracted is None:
        return []

    if extracted == registered:
        return []

    # Hash mismatch (Requirement 7.5).
    return [
        _indicator(
            claim_id,
            reason=REASON_HASH_MISMATCH,
            title="ROE signature does not match employer certificate registry",
            explanation_en=(
                "The digital signature embedded in the submitted ROE document "
                "does not match the employer's registered X.509 certificate "
                "hash in the EI-ADS employer signature registry."
            ),
            explanation_fr=(
                "La signature numerique integree dans le releve d'emploi soumis "
                "ne correspond pas a l'empreinte du certificat X.509 enregistre "
                "de l'employeur dans le registre des signatures d'EI-ADS."
            ),
            payload={
                "flag": REASON_HASH_MISMATCH,
                "signature_present": True,
                "observed_cert_hash": extracted,
                "registered_cert_hash": registered,
            },
        )
    ]


# ---------------------------------------------------------------------------
# cryptography handling (isolated so the sandbox can run without it)
# ---------------------------------------------------------------------------


def _cryptography_available() -> bool:
    try:  # pragma: no cover - depends on environment
        import cryptography  # noqa: F401

        return True
    except ImportError:  # pragma: no cover
        return False


def _extract_hash_via_cryptography(raw: bytes) -> Optional[str]:
    """Load a PKCS#7 signature and return the signer cert SHA-256 hex, or None."""
    try:  # pragma: no cover - exercised in envs with cryptography
        from cryptography.hazmat.primitives.serialization import pkcs7
    except ImportError:  # pragma: no cover - sandbox without cryptography
        return None

    certs = None
    # PKCS#7 SignedData may be DER or PEM encoded. Try both loaders.
    for loader in (
        getattr(pkcs7, "load_der_pkcs7_certificates", None),
        getattr(pkcs7, "load_pem_pkcs7_certificates", None),
    ):
        if loader is None:
            continue
        try:
            candidate = loader(raw)
        except Exception:  # noqa: BLE001 - wrong encoding; try the next loader
            continue
        if candidate:
            certs = candidate
            break

    if not certs:
        return None

    signer = certs[0]
    try:
        from cryptography.hazmat.primitives import hashes

        fingerprint = signer.fingerprint(hashes.SHA256())
    except Exception:  # noqa: BLE001 - defensive
        return None
    return fingerprint.hex().lower()


def _resolve_signature_bytes(document: dict[str, Any]) -> Optional[bytes]:
    """Resolve raw PKCS#7 signature bytes from the document payload."""
    for key in ("signature_pkcs7", "signature", "signature_der"):
        value = document.get(key)
        if value is None:
            continue
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        if isinstance(value, str):
            # Accept base64-encoded signature material.
            try:
                return base64.b64decode(value, validate=False)
            except Exception:  # noqa: BLE001 - not base64; treat as raw text bytes
                return value.encode("latin-1", errors="ignore")
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sha256_certificate_hash(certificate_der: Union[bytes, bytearray]) -> str:
    """Return the SHA-256 fingerprint (lowercase hex) of DER-encoded cert bytes.

    A small convenience used by tests / registry seeding to compute the same
    fingerprint form that :func:`extract_signature_certificate_hash` produces.
    """
    return hashlib.sha256(bytes(certificate_der)).hexdigest().lower()


def _indicator(
    claim_id: Optional[str],
    *,
    reason: str,
    title: str,
    explanation_en: str,
    explanation_fr: str,
    payload: dict[str, Any],
) -> Indicator:
    return Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=_coerce_uuid(claim_id),
        engine=EngineType.DOCUMENT_FORGERY,
        severity=Severity.CRITICAL,
        algorithm="digital_signature_verification",
        title=title,
        explanation_en=explanation_en,
        explanation_fr=explanation_fr,
        payload=payload,
        produced_at=datetime.now(timezone.utc),
    )


def _coerce_uuid(claim_id: Optional[str]) -> uuid.UUID:
    """Coerce a claim id string to a UUID, tolerating missing / non-UUID ids."""
    if isinstance(claim_id, uuid.UUID):
        return claim_id
    if claim_id:
        try:
            return uuid.UUID(str(claim_id))
        except (ValueError, AttributeError, TypeError):
            return uuid.uuid5(uuid.NAMESPACE_OID, str(claim_id))
    return uuid.uuid4()


__all__ = [
    "REASON_NO_SIGNATURE",
    "REASON_HASH_MISMATCH",
    "SignatureExtraction",
    "extract_signature_certificate_hash",
    "check_digital_signature",
    "sha256_certificate_hash",
]

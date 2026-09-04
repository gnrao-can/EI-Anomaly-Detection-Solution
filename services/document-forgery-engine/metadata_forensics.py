"""
services/document-forgery-engine/metadata_forensics.py
======================================================
PDF metadata forensics for the Document Forgery Engine (task 15.2).

Design step 3 of the Document Forgery Engine (design "5. Document Forgery
Engine") requires extracting the PDF *creation timestamp* from the document's
metadata and comparing it to the ROE's declared *issue date*. When the PDF was
created more than 24 hours after the ROE claims to have been issued, the
document was almost certainly fabricated / re-generated after the fact, so a
CRITICAL Indicator is produced naming *both* timestamps (Requirement 7.3).

Extraction uses ``pypdf`` (pinned ``5.1.0`` in
``scripts/dependencies/python-versions.toml``). ``pypdf`` exposes the parsed
``/CreationDate`` as :attr:`pypdf.DocumentInformation.creation_date`
(a timezone-aware :class:`datetime`), which we use directly.

Sandbox / dependency fallback
------------------------------
The sandbox may not have ``pypdf`` installed. To keep the engine importable and
testable everywhere, this module isolates the pypdf dependency behind
:func:`extract_pdf_creation_timestamp`, which:

  * uses ``pypdf`` when it is importable, and
  * otherwise falls back to a small, dependency-free ``/CreationDate`` scanner
    (:func:`_scan_creation_date`) that parses the standard PDF date syntax
    ``D:YYYYMMDDHHmmSS(+/-)HH'mm'`` straight out of the raw bytes.

Both paths return an aware ``datetime`` (or ``None`` when no creation date is
present). Callers may also supply the creation timestamp directly (already
parsed upstream) so the forensics check itself never needs pypdf.

The boundary is strict on the "> 24h" side per the requirement wording:
  * creation exactly issue_date + 24h        -> NO Indicator
  * creation issue_date + 24h + 1s           -> CRITICAL Indicator
  * creation before / equal to issue_date    -> NO Indicator

Requirements: 7.3
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union

from shared.models import EngineType, Indicator, Severity

#: Grace window between the ROE's declared issue date and the PDF creation
#: timestamp. A creation timestamp *strictly after* ``issue_date + 24h`` is
#: flagged CRITICAL (Requirement 7.3).
CREATION_GRACE = timedelta(hours=24)

# Standard PDF date string: D:YYYYMMDDHHmmSSOHH'mm' where O is +, -, or Z.
# All components after the year are optional per the PDF spec.
_PDF_DATE_RE = re.compile(
    r"D:(?P<year>\d{4})"
    r"(?P<month>\d{2})?"
    r"(?P<day>\d{2})?"
    r"(?P<hour>\d{2})?"
    r"(?P<minute>\d{2})?"
    r"(?P<second>\d{2})?"
    r"(?P<tz>[Zz]|[+\-]\d{2}'?\d{2}'?)?"
)


def extract_pdf_creation_timestamp(
    pdf: Union[bytes, str, Any],
) -> Optional[datetime]:
    """Extract the PDF ``/CreationDate`` as a timezone-aware ``datetime``.

    Accepts raw PDF ``bytes``, a filesystem path (``str``), or a file-like
    object. Prefers ``pypdf`` when available and falls back to a raw-bytes
    ``/CreationDate`` scanner otherwise. Returns ``None`` when no creation date
    can be found.
    """
    reader = _try_pypdf_reader(pdf)
    if reader is not None:
        info = getattr(reader, "metadata", None)
        creation = getattr(info, "creation_date", None) if info else None
        if isinstance(creation, datetime):
            return _ensure_aware(creation)
        # pypdf present but no parseable creation_date -> try raw scan as a
        # secondary path (handles non-standard producers).

    raw = _read_bytes(pdf)
    if raw is None:
        return None
    return _scan_creation_date(raw)


def check_pdf_creation_vs_issue_date(
    creation_timestamp: Optional[datetime],
    issue_date: Optional[datetime],
    *,
    claim_id: Optional[str] = None,
    grace: timedelta = CREATION_GRACE,
) -> list[Indicator]:
    """Flag a PDF that was created well after the ROE claims to be issued.

    Produces a single CRITICAL Indicator when::

        creation_timestamp > issue_date + grace   (grace defaults to 24h)

    Both timestamps are recorded in the Indicator (payload + explanation) per
    Requirement 7.3. The comparison is strict, so a creation timestamp exactly
    ``issue_date + 24h`` does NOT flag; one second later does.

    When either timestamp is missing the check is skipped (no Indicator): the
    creation timestamp may be absent from the PDF, and the issue date is a
    separate OCR field validated elsewhere.
    """
    if creation_timestamp is None or issue_date is None:
        return []

    creation = _ensure_aware(creation_timestamp)
    issued = _ensure_aware(issue_date)
    cutoff = issued + grace

    if creation <= cutoff:
        return []

    delta = creation - issued
    payload = {
        "flag": "PDF_CREATED_AFTER_ISSUE",
        "creation_timestamp": creation.isoformat(),
        "issue_date": issued.isoformat(),
        "grace_hours": grace.total_seconds() / 3600.0,
        "delta_hours": round(delta.total_seconds() / 3600.0, 4),
    }
    indicator = Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=_coerce_uuid(claim_id),
        engine=EngineType.DOCUMENT_FORGERY,
        severity=Severity.CRITICAL,
        algorithm="pdf_creation_timestamp_forensics",
        title="ROE PDF created after its declared issue date",
        explanation_en=(
            f"The ROE PDF creation timestamp ({creation.isoformat()}) post-dates "
            f"the declared issue date ({issued.isoformat()}) by more than "
            f"{grace.total_seconds() / 3600.0:.0f} hours, indicating the "
            f"document may have been fabricated or regenerated after issuance."
        ),
        explanation_fr=(
            f"L'horodatage de creation du PDF du releve d'emploi "
            f"({creation.isoformat()}) est posterieur a la date d'emission "
            f"declaree ({issued.isoformat()}) de plus de "
            f"{grace.total_seconds() / 3600.0:.0f} heures, ce qui indique que "
            f"le document a pu etre fabrique ou regenere apres l'emission."
        ),
        payload=payload,
        produced_at=datetime.now(timezone.utc),
    )
    return [indicator]


# ---------------------------------------------------------------------------
# pypdf handling (isolated so the sandbox can run without it)
# ---------------------------------------------------------------------------


def _try_pypdf_reader(pdf: Union[bytes, str, Any]) -> Optional[Any]:
    """Return a ``pypdf.PdfReader`` for ``pdf`` when pypdf is importable."""
    try:  # pragma: no cover - exercised in envs with pypdf
        from pypdf import PdfReader  # type: ignore
    except ImportError:  # pragma: no cover - sandbox without pypdf
        return None

    try:
        if isinstance(pdf, (bytes, bytearray)):
            import io

            return PdfReader(io.BytesIO(bytes(pdf)))
        # str path or file-like object are both accepted by PdfReader.
        return PdfReader(pdf)
    except Exception:  # pragma: no cover - malformed PDF; fall back to raw scan
        return None


def _read_bytes(pdf: Union[bytes, str, Any]) -> Optional[bytes]:
    """Best-effort read of the PDF content as raw bytes for the fallback scan."""
    if isinstance(pdf, (bytes, bytearray)):
        return bytes(pdf)
    if isinstance(pdf, str):
        try:
            with open(pdf, "rb") as handle:
                return handle.read()
        except OSError:
            return None
    read = getattr(pdf, "read", None)
    if callable(read):
        try:
            seek = getattr(pdf, "seek", None)
            if callable(seek):
                pdf.seek(0)
            data = read()
            return data if isinstance(data, (bytes, bytearray)) else None
        except Exception:  # pragma: no cover - defensive
            return None
    return None


def _scan_creation_date(raw: bytes) -> Optional[datetime]:
    """Dependency-free ``/CreationDate`` extraction from raw PDF bytes."""
    try:
        text = raw.decode("latin-1", errors="ignore")
    except Exception:  # pragma: no cover - defensive
        return None
    marker = text.find("/CreationDate")
    if marker == -1:
        return None
    match = _PDF_DATE_RE.search(text, marker)
    if match is None:
        return None
    return _parse_pdf_date(match)


def _parse_pdf_date(match: "re.Match[str]") -> Optional[datetime]:
    """Build an aware ``datetime`` from a matched PDF date string."""
    parts = match.groupdict()
    try:
        year = int(parts["year"])
        month = int(parts["month"] or "1")
        day = int(parts["day"] or "1")
        hour = int(parts["hour"] or "0")
        minute = int(parts["minute"] or "0")
        second = int(parts["second"] or "0")
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None

    tzinfo = timezone.utc
    tz = parts.get("tz")
    if tz and tz not in ("Z", "z"):
        sign = 1 if tz[0] == "+" else -1
        digits = re.sub(r"[^\d]", "", tz[1:])
        if len(digits) >= 2:
            tz_hours = int(digits[:2])
            tz_minutes = int(digits[2:4]) if len(digits) >= 4 else 0
            tzinfo = timezone(sign * timedelta(hours=tz_hours, minutes=tz_minutes))

    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=tzinfo)
    except ValueError:  # pragma: no cover - defensive
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_aware(value: datetime) -> datetime:
    """Treat naive datetimes as UTC so comparisons never raise."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


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
    "CREATION_GRACE",
    "extract_pdf_creation_timestamp",
    "check_pdf_creation_vs_issue_date",
]

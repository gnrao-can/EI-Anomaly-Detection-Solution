"""
services/document-forgery-engine/font_fingerprint.py
====================================================
Font fingerprinting for the Document Forgery Engine (task 15.2).

Design step 4 of the Document Forgery Engine (design "5. Document Forgery
Engine") requires extracting the *font family* and *point size* used in a
submitted ROE PDF and comparing them to the font profile established from the
employer's prior authenticated ROE documents in the EI-ADS employer document
registry. A mismatch of either the font family or the point size produces a
CRITICAL Indicator naming the expected and observed values (Requirement 7.4).

The font check is *skipped* when the employer has fewer than 3 prior
authenticated ROEs, because a font profile cannot be reliably established from
too few samples (task 15.2 constraint / design step 4 "skip if < 3 prior
authenticated ROEs").

Extraction uses ``pdfminer.six`` (pinned ``20231228`` in
``scripts/dependencies/python-versions.toml``). We walk the layout tree of the
first page(s), collect every :class:`~pdfminer.layout.LTChar` glyph, and take:

  * the *dominant font family* — the most frequently occurring ``fontname``
    (subset prefixes such as ``ABCDEF+Arial`` are stripped to ``Arial``); and
  * the *dominant point size* — the most frequent rounded glyph ``size``.

Sandbox / dependency fallback
------------------------------
When ``pdfminer.six`` is not importable, :func:`extract_font_profile` returns
``None`` and the caller skips the font check gracefully. Callers may also supply
an already-extracted :class:`FontProfile` directly, so the forensics logic is
fully testable without pdfminer.

Requirements: 7.4
"""

from __future__ import annotations

import io
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Union

from shared.models import EngineType, Indicator, Severity

#: Minimum number of prior authenticated ROEs an employer must have before a
#: font profile is considered established enough to compare against
#: (Requirement 7.4 / design step 4). Fewer than this -> skip the check.
MIN_AUTHENTICATED_ROES_FOR_FONT_CHECK = 3

#: Absolute tolerance (points) when comparing observed vs expected point size,
#: to absorb sub-point rounding differences from the PDF renderer.
POINT_SIZE_TOLERANCE = 0.5


@dataclass(frozen=True)
class FontProfile:
    """The dominant font family and point size of a rendered PDF / ROE.

    Attributes
    ----------
    font_family:
        The dominant font family name, with any ``ABCDEF+`` subset prefix
        stripped (e.g. ``"Helvetica"``, ``"Arial"``).
    point_size:
        The dominant glyph point size (rounded to 2 decimal places).
    """

    font_family: str
    point_size: float


def extract_font_profile(
    pdf: Union[bytes, str, Any],
    *,
    max_pages: int = 1,
) -> Optional[FontProfile]:
    """Extract the dominant :class:`FontProfile` from a PDF using pdfminer.six.

    Returns ``None`` when pdfminer.six is unavailable or no text glyphs are
    found (e.g. a scanned/image-only PDF). ``max_pages`` bounds the work for
    latency (design: the engine must publish within 700 ms).
    """
    try:  # pragma: no cover - exercised in envs with pdfminer.six
        from pdfminer.high_level import extract_pages  # type: ignore
        from pdfminer.layout import LAParams, LTChar, LTContainer  # type: ignore
    except ImportError:  # pragma: no cover - sandbox without pdfminer.six
        return None

    stream = _as_stream(pdf)
    if stream is None:
        return None

    font_counter: Counter[str] = Counter()
    size_counter: Counter[float] = Counter()

    def _walk(obj: Any):
        if isinstance(obj, LTChar):
            font_counter[_normalize_font_name(obj.fontname)] += 1
            size_counter[round(float(obj.size), 2)] += 1
        if isinstance(obj, LTContainer):
            for child in obj:
                _walk(child)

    try:
        for index, page in enumerate(extract_pages(stream, laparams=LAParams())):
            if index >= max_pages:
                break
            _walk(page)
    except Exception:  # pragma: no cover - malformed PDF
        return None

    if not font_counter or not size_counter:
        return None

    dominant_font = font_counter.most_common(1)[0][0]
    dominant_size = size_counter.most_common(1)[0][0]
    return FontProfile(font_family=dominant_font, point_size=dominant_size)


def check_font_profile(
    observed: Optional[FontProfile],
    expected: Optional[FontProfile],
    *,
    prior_authenticated_roe_count: int,
    claim_id: Optional[str] = None,
    point_size_tolerance: float = POINT_SIZE_TOLERANCE,
) -> list[Indicator]:
    """Flag a font family / point size mismatch against the employer profile.

    Produces a single CRITICAL Indicator when the observed font family differs
    from the expected family (case-insensitive) OR the observed point size
    differs from the expected size by more than ``point_size_tolerance``
    (Requirement 7.4). The Indicator names both the expected and observed
    family and size.

    The check is SKIPPED (returns ``[]``) when:
      * the employer has fewer than
        :data:`MIN_AUTHENTICATED_ROES_FOR_FONT_CHECK` prior authenticated ROEs
        (design step 4 / Requirement 7.4 profile cannot be established), or
      * either the observed or expected profile is unavailable (``None``).
    """
    if prior_authenticated_roe_count < MIN_AUTHENTICATED_ROES_FOR_FONT_CHECK:
        return []
    if observed is None or expected is None:
        return []

    family_mismatch = (
        observed.font_family.strip().lower() != expected.font_family.strip().lower()
    )
    size_mismatch = abs(observed.point_size - expected.point_size) > point_size_tolerance

    if not family_mismatch and not size_mismatch:
        return []

    payload = {
        "flag": "FONT_PROFILE_MISMATCH",
        "expected_font_family": expected.font_family,
        "observed_font_family": observed.font_family,
        "expected_point_size": expected.point_size,
        "observed_point_size": observed.point_size,
        "family_mismatch": family_mismatch,
        "size_mismatch": size_mismatch,
        "prior_authenticated_roe_count": prior_authenticated_roe_count,
    }
    indicator = Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=_coerce_uuid(claim_id),
        engine=EngineType.DOCUMENT_FORGERY,
        severity=Severity.CRITICAL,
        algorithm="font_fingerprint",
        title="ROE font does not match employer document registry profile",
        explanation_en=(
            f"The ROE font profile (family '{observed.font_family}', size "
            f"{observed.point_size}pt) differs from the employer's established "
            f"profile (family '{expected.font_family}', size "
            f"{expected.point_size}pt) built from {prior_authenticated_roe_count} "
            f"prior authenticated ROEs."
        ),
        explanation_fr=(
            f"Le profil de police du releve d'emploi (famille "
            f"'{observed.font_family}', taille {observed.point_size}pt) differe "
            f"du profil etabli de l'employeur (famille '{expected.font_family}', "
            f"taille {expected.point_size}pt) construit a partir de "
            f"{prior_authenticated_roe_count} releves d'emploi authentifies "
            f"anterieurs."
        ),
        payload=payload,
        produced_at=datetime.now(timezone.utc),
    )
    return [indicator]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_font_name(fontname: str) -> str:
    """Strip a PDF subset prefix (``ABCDEF+Arial`` -> ``Arial``)."""
    if not fontname:
        return fontname
    if "+" in fontname and fontname.index("+") == 6:
        return fontname.split("+", 1)[1]
    return fontname


def _as_stream(pdf: Union[bytes, str, Any]) -> Optional[Any]:
    """Return a file path or seekable binary stream that pdfminer accepts."""
    if isinstance(pdf, (bytes, bytearray)):
        return io.BytesIO(bytes(pdf))
    if isinstance(pdf, str):
        return pdf  # extract_pages accepts a path string
    if hasattr(pdf, "read"):
        seek = getattr(pdf, "seek", None)
        if callable(seek):
            try:
                pdf.seek(0)
            except Exception:  # pragma: no cover - defensive
                pass
        return pdf
    return None


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
    "MIN_AUTHENTICATED_ROES_FOR_FONT_CHECK",
    "POINT_SIZE_TOLERANCE",
    "FontProfile",
    "extract_font_profile",
    "check_font_profile",
]

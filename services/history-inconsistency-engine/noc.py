"""
services/history-inconsistency-engine/noc.py
=============================================
NOC (National Occupational Classification) validation for the Employment
History Inconsistency Engine (task 14.2).

The engine cross-references the NOC code the claimant declared on their ROE
against the employer industry category the CRA holds on file for the same
employer Business Number. A mismatch at the NOC *major group* level is a fraud
signal: it suggests the claimant's declared occupation is inconsistent with the
kind of work the employer actually reports to the CRA.

Design ("Employment History Inconsistency Engine", step 3):

    3. NOC code validation (CRA employer industry category):
       - Map CRA employer industry category to NOC major group
       - If declared NOC major group != CRA industry NOC major group -> HIGH Indicator

NOC major group
---------------
The NOC 2021 taxonomy is hierarchical. The first digit of a 5-digit NOC code is
the *broad category*; the first two digits are the *major group*. This module
derives the major group as the first two digits of a NOC code, tolerating
loosely-formatted inputs (whitespace, a trailing letter, fewer digits).

The CRA "employer industry category" arrives either as a NOC code itself or as a
single-digit/short industry category. :func:`noc_major_group` normalises both to
a comparable major-group token, and :func:`check_noc_mismatch` produces the HIGH
Indicator only when both sides resolve to a comparable value and they differ
(Requirement 6.5). When either side cannot be resolved to a NOC major group, no
Indicator is produced (we do not flag on missing data — that is handled as
UNRESOLVED elsewhere).

Requirements: 6.5
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from shared.models import EngineType, Indicator, Severity

_DIGITS = re.compile(r"\d+")


def normalise_noc_digits(value: Optional[str]) -> Optional[str]:
    """Extract the leading run of digits from a NOC-like token.

    Returns the digit string (e.g. ``"7263"`` from ``" 7263 "`` or ``"NOC7263"``)
    or ``None`` when *value* contains no digits. This lets us accept CRA industry
    categories that carry a prefix/suffix or stray formatting.
    """
    if value is None:
        return None
    match = _DIGITS.search(str(value))
    if match is None:
        return None
    return match.group(0)


def noc_major_group(value: Optional[str]) -> Optional[str]:
    """Return the NOC *major group* (first two digits) for a NOC-like token.

    The NOC major group is the first two digits of a 5-digit NOC 2021 code. For
    inputs shorter than two digits (e.g. a single-digit CRA industry category)
    the available digit(s) are returned so a broad-category comparison is still
    possible. Returns ``None`` when no digits are present.

    Examples
    --------
    ``"72630"`` -> ``"72"``   ``"7263"`` -> ``"72"``   ``"7"`` -> ``"7"``
    ``" 21231 "`` -> ``"21"``  ``"abc"`` -> ``None``
    """
    digits = normalise_noc_digits(value)
    if not digits:
        return None
    return digits[:2] if len(digits) >= 2 else digits


def noc_groups_match(declared: Optional[str], cra_category: Optional[str]) -> bool:
    """Return ``True`` when the two NOC-like tokens share a major group.

    The comparison is done at the *major group* (two-digit) level. When either
    side resolves to only a single digit (a broad CRA industry category), the
    comparison falls back to the common broad-category (first-digit) level so a
    single-digit category is not spuriously reported as a mismatch against a
    full NOC code that starts with the same digit.
    """
    a = noc_major_group(declared)
    b = noc_major_group(cra_category)
    if a is None or b is None:
        # Cannot resolve one side -> treat as "no mismatch" (do not flag on
        # missing data; UNRESOLVED handling covers absent inputs elsewhere).
        return True
    # If one side is only a single digit, compare at the broad-category level.
    if len(a) == 1 or len(b) == 1:
        return a[0] == b[0]
    return a == b


def check_noc_mismatch(
    declared_noc: Optional[str],
    cra_industry_category: Optional[str],
    *,
    claim_id: Optional[str] = None,
) -> list[Indicator]:
    """Return a HIGH Indicator when the declared NOC and CRA category differ.

    Produces a single HIGH-severity Indicator (Requirement 6.5) when both the
    declared NOC code and the CRA employer industry category resolve to a NOC
    major group and those major groups differ. When either side cannot be
    resolved to a NOC major group, or when they match, the returned list is
    empty.

    The Indicator payload cites *both* the declared and actual classifications
    (Requirement 6.5) at the raw and major-group levels for the audit trail.
    """
    if noc_groups_match(declared_noc, cra_industry_category):
        return []

    declared_group = noc_major_group(declared_noc)
    cra_group = noc_major_group(cra_industry_category)

    indicator = Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=_coerce_uuid(claim_id),
        engine=EngineType.HISTORY_INCONSISTENCY,
        severity=Severity.HIGH,
        algorithm="noc_industry_validation",
        title="Declared NOC does not match CRA employer industry category",
        explanation_en=(
            f"The NOC code declared on the ROE ({declared_noc}, major group "
            f"{declared_group}) does not match the employer industry "
            f"category on file with the CRA ({cra_industry_category}, major "
            f"group {cra_group})."
        ),
        explanation_fr=(
            f"Le code CNP declare sur le releve d'emploi ({declared_noc}, "
            f"groupe majeur {declared_group}) ne correspond pas a la categorie "
            f"d'industrie de l'employeur enregistree aupres de l'ARC "
            f"({cra_industry_category}, groupe majeur {cra_group})."
        ),
        payload={
            "declared_noc": str(declared_noc) if declared_noc is not None else None,
            "declared_noc_major_group": declared_group,
            "cra_industry_category": (
                str(cra_industry_category)
                if cra_industry_category is not None
                else None
            ),
            "cra_noc_major_group": cra_group,
        },
        produced_at=datetime.now(timezone.utc),
    )
    return [indicator]


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
    "normalise_noc_digits",
    "noc_major_group",
    "noc_groups_match",
    "check_noc_mismatch",
]

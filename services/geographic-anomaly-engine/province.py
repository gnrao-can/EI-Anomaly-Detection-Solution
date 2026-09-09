"""
services/geographic-anomaly-engine/province.py
===============================================
Derive the claimant's province from their residential postal-code FSA.

The Geographic Anomaly Engine keys its MVR (Req 4.3 / 15.1) and CBSA
(Req 4.9 / 13.x) lookups on the claimant's residential PROVINCE. The engine
receives the claimant's residential postal code as an FSA (the first three
characters of the Canadian postal code — the only portion retained under
Protected B data minimisation; see ``shared.models.Claim.residential_postal_code``
and the ROE Adapter normaliser). No separate province field is carried on the
``claim.received`` event.

Req 15.1 assigns the "province matching the claimant's residential address"
responsibility to the MVR lookup; the engine owns that address→province step.
The FIRST LETTER of a Canadian FSA identifies the province/territory of the
address under the standard Canada Post convention, so we derive the province
from the FSA rather than inventing a new event field. When the caller supplies
an explicit ``residential_province`` it is always preferred; this derivation is
only a fallback for when it is absent (the common case for events that carry
only the FSA).

Canada Post FSA first-letter → province/territory (standard public convention):

    A  NL     B  NS     C  PE     E  NB     G,H,J  QC     K,L,M,N,P  ON
    R  MB     S  SK     T  AB     V  BC     X  NT/NU      Y  YT

Letters D, F, I, O, Q, U, W, Z are not used as the first character of any
Canadian FSA. ``X`` is shared by the Northwest Territories and Nunavut; we map
it to ``NT`` because the MVR/CBSA mock fixtures and provincial-registry set use
NT as the representative northern jurisdiction (no separate NU fixture exists).

This module is pure (no I/O) so it is exhaustively unit-testable.

Requirements: 4.3, 4.9, 15.1
"""

from __future__ import annotations

from typing import Optional

#: Canada Post FSA first-letter → province/territory 2-letter code.
_FSA_FIRST_LETTER_TO_PROVINCE: dict[str, str] = {
    "A": "NL",  # Newfoundland and Labrador
    "B": "NS",  # Nova Scotia
    "C": "PE",  # Prince Edward Island
    "E": "NB",  # New Brunswick
    "G": "QC",  # Quebec (eastern)
    "H": "QC",  # Quebec (metropolitan Montreal)
    "J": "QC",  # Quebec (western)
    "K": "ON",  # Ontario (eastern)
    "L": "ON",  # Ontario (central)
    "M": "ON",  # Ontario (Metropolitan Toronto)
    "N": "ON",  # Ontario (southwestern)
    "P": "ON",  # Ontario (northern)
    "R": "MB",  # Manitoba
    "S": "SK",  # Saskatchewan
    "T": "AB",  # Alberta
    "V": "BC",  # British Columbia
    "X": "NT",  # Northwest Territories / Nunavut (see module docstring)
    "Y": "YT",  # Yukon
}


def province_from_fsa(postal_code: Optional[str]) -> str:
    """Return the 2-letter province/territory code for a residential FSA.

    Uses the first letter of the (normalised) FSA per the Canada Post
    convention documented in this module. Returns ``""`` when the input is
    empty or its first letter is not a valid Canadian FSA letter (D/F/I/O/Q/U/
    W/Z or any non-letter) — the engine then performs its MVR/CBSA lookups with
    an empty province, which the adapters treat as an unknown/benign profile
    (no fabricated province is ever substituted).

    Parameters
    ----------
    postal_code:
        The claimant's residential postal code or FSA (only the first character
        is significant). Case- and whitespace-insensitive.

    Returns
    -------
    str
        A 2-letter province/territory code (e.g. ``"ON"``), or ``""`` when the
        FSA cannot be mapped.
    """
    if not postal_code:
        return ""
    first = str(postal_code).strip().upper()[:1]
    return _FSA_FIRST_LETTER_TO_PROVINCE.get(first, "")


def resolve_province(
    explicit_province: Optional[str], residential_postal_code: Optional[str]
) -> str:
    """Resolve the province to key MVR/CBSA lookups on.

    Prefers an explicitly supplied ``residential_province`` (e.g. a test or a
    future event field); otherwise derives it from the residential FSA via
    :func:`province_from_fsa`. Returns ``""`` when neither yields a province.
    """
    if explicit_province:
        return str(explicit_province).strip().upper()
    return province_from_fsa(residential_postal_code)


__all__ = [
    "province_from_fsa",
    "resolve_province",
]

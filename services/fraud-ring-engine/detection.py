"""
services/fraud-ring-engine/detection.py
========================================
Pure detection functions for the Fraud Ring Engine (task 13.1).

Two independent, dependency-light, deterministic detectors:

1. ``check_shell_employer(registration, separation_date, ...)``
   Detects "shell" employers used to manufacture fraudulent ROEs
   (Requirement 5.2):
     - CRITICAL when the employer was registered within 12 months BEFORE the
       separation date AND the registered address is RESIDENTIAL.
     - HIGH when there is NO registry record for the employer at all.
   Any other case (registered longer ago, or a commercial/unknown address)
   produces no Indicator.

2. ``check_coordinated_layoff(cohort, ...)``
   Detects coordinated mass layoffs across the 90-day separation cohort
   (Requirements 5.3, 5.4):
     - CRITICAL when >= 80% of the cohort share the same separation calendar
       date AND all of their ROE submission timestamps fall within ANY rolling
       24-hour window.
   Boundary behaviour: 79% share the date -> no flag; 80% -> flag (when the ROE
   window condition also holds).

The functions are pure (no I/O) and operate on already-retrieved data. The
caller (``main.py`` / ``client.py``) is responsible for fetching the employer
registration and the cohort.

Requirements: 5.2, 5.3, 5.4
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional, Sequence

from shared.models import EngineType, Indicator, Severity

__all__ = [
    "DEFAULT_SHELL_REGISTRATION_WINDOW_MONTHS",
    "DEFAULT_COORDINATED_SAME_DATE_FRACTION",
    "DEFAULT_COORDINATED_ROE_WINDOW_HOURS",
    "CohortMember",
    "check_shell_employer",
    "check_coordinated_layoff",
]

# Design thresholds (Requirements 5.2–5.4).
DEFAULT_SHELL_REGISTRATION_WINDOW_MONTHS = 12
DEFAULT_COORDINATED_SAME_DATE_FRACTION = 0.80
DEFAULT_COORDINATED_ROE_WINDOW_HOURS = 24

# Approximate days per month for the 12-month registration window. A fixed
# 30.4375-day month (365.25 / 12) keeps the window free of calendar-arithmetic
# edge cases while remaining accurate to within a day or so.
_DAYS_PER_MONTH = 365.25 / 12.0


# ---------------------------------------------------------------------------
# Cohort member (input shape for coordinated-layoff detection)
# ---------------------------------------------------------------------------


@dataclass
class CohortMember:
    """One employee separated from the employer within the 90-day cohort window.

    Attributes
    ----------
    sin_hash:
        SHA-256 hash of the employee's SIN (identity within the cohort).
    separation_date:
        Calendar date on which employment ended.
    roe_submission_ts:
        UTC timestamp at which the employee's ROE was submitted.
    claim_id:
        Optional claim identifier, when the cohort member has an associated
        claim.
    """

    sin_hash: str
    separation_date: date
    roe_submission_ts: datetime
    claim_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Indicator helper
# ---------------------------------------------------------------------------


def _coerce_claim_uuid(claim_id: Optional[str]) -> uuid.UUID:
    """Return a UUID for the Indicator's ``claim_id`` field.

    The Indicator model types ``claim_id`` as a UUID. When the caller passes a
    non-UUID string (or ``None``), we substitute a deterministic-enough UUID so
    the model stays well-typed without raising.
    """
    if isinstance(claim_id, uuid.UUID):
        return claim_id
    if claim_id:
        try:
            return uuid.UUID(str(claim_id))
        except (ValueError, AttributeError, TypeError):
            return uuid.uuid5(uuid.NAMESPACE_OID, str(claim_id))
    return uuid.uuid4()


def _new_indicator(
    *,
    claim_id: Optional[str],
    severity: Severity,
    algorithm: str,
    title: str,
    explanation_en: str,
    explanation_fr: str,
    payload: dict,
) -> Indicator:
    """Build a FRAUD_RING Indicator with a fresh id + UTC timestamp."""
    return Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=_coerce_claim_uuid(claim_id),
        engine=EngineType.FRAUD_RING,
        severity=severity,
        algorithm=algorithm,
        title=title[:100],
        explanation_en=explanation_en[:500],
        explanation_fr=explanation_fr[:500],
        payload=payload,
        produced_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Registration shape helpers
# ---------------------------------------------------------------------------


def _registration_date(registration: Any) -> Optional[date]:
    """Extract a ``registration_date`` (``date``) from an EmployerRegistration.

    Accepts either an ``EmployerRegistration`` dataclass (attribute access) or a
    plain ``dict`` (as returned over the wire by the Business Registry Adapter).
    Returns ``None`` when the value is missing or unparseable.
    """
    raw = None
    if registration is None:
        return None
    if isinstance(registration, dict):
        raw = registration.get("registration_date")
    else:
        raw = getattr(registration, "registration_date", None)

    if raw is None or raw == "":
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return None


def _address_type_str(registration: Any) -> str:
    """Extract the address type as an upper-case string ("RESIDENTIAL", ...)."""
    if registration is None:
        return "UNKNOWN"
    if isinstance(registration, dict):
        raw = registration.get("address_type", "UNKNOWN")
    else:
        raw = getattr(registration, "address_type", "UNKNOWN")
    # AddressType enums stringify to their ".value" via ``.value``; also support
    # a bare Enum or a plain string.
    value = getattr(raw, "value", raw)
    return str(value).strip().upper()


def _is_residential(registration: Any) -> bool:
    return _address_type_str(registration) == "RESIDENTIAL"


# ---------------------------------------------------------------------------
# 1. Shell-employer detection (Req 5.2)
# ---------------------------------------------------------------------------


def check_shell_employer(
    registration: Any,
    separation_date: date,
    *,
    window_months: int = DEFAULT_SHELL_REGISTRATION_WINDOW_MONTHS,
    claim_id: Optional[str] = None,
    employer_bn: Optional[str] = None,
) -> list[Indicator]:
    """Flag a shell employer based on registration recency + address zoning.

    Parameters
    ----------
    registration:
        The employer's canonical registration record (an
        ``EmployerRegistration`` dataclass or an equivalent ``dict`` with
        ``registration_date`` and ``address_type`` keys), or ``None`` when no
        registry record was found for the employer BN.
    separation_date:
        The separation date of the claim under analysis.
    window_months:
        Recency window in months (default 12, Req 5.2). A CRITICAL Indicator
        requires the registration date to fall within this many months BEFORE
        the separation date.
    claim_id:
        Optional id of the claim under analysis, attached to the Indicator.
    employer_bn:
        Optional employer BN recorded on the Indicator payload for evidence.

    Returns
    -------
    list[Indicator]
        - ``[HIGH]``     when no registry record exists (``registration`` is
          ``None``).
        - ``[CRITICAL]`` when the employer was registered within ``window_months``
          before the separation date AND the registered address is residential.
        - ``[]``         otherwise.
    """
    # ── No registry record at all -> HIGH (Req 5.2) ──
    if registration is None:
        return [
            _new_indicator(
                claim_id=claim_id,
                severity=Severity.HIGH,
                algorithm="shell_employer_no_record",
                title="No business registry record for employer",
                explanation_en=(
                    "No federal or provincial business registry record was found "
                    "for the employer named on this claim, which is consistent "
                    "with a shell employer created to manufacture fraudulent "
                    "Records of Employment."
                ),
                explanation_fr=(
                    "Aucun enregistrement d'entreprise federal ou provincial n'a "
                    "ete trouve pour l'employeur nomme sur cette demande, ce qui "
                    "correspond a un employeur fictif cree pour fabriquer de faux "
                    "releves d'emploi."
                ),
                payload={
                    "employer_bn": employer_bn,
                    "reason": "NO_REGISTRY_RECORD",
                },
            )
        ]

    reg_date = _registration_date(registration)
    residential = _is_residential(registration)

    # Registration recency: registered within ``window_months`` BEFORE the
    # separation date. A registration AFTER the separation date, or one older
    # than the window, does not qualify.
    recent = False
    months_before: Optional[float] = None
    if reg_date is not None:
        days_before = (separation_date - reg_date).days
        if days_before >= 0:
            months_before = days_before / _DAYS_PER_MONTH
            window_days = window_months * _DAYS_PER_MONTH
            recent = days_before <= window_days

    # ── Registered recently AND residential -> CRITICAL (Req 5.2) ──
    if recent and residential:
        return [
            _new_indicator(
                claim_id=claim_id,
                severity=Severity.CRITICAL,
                algorithm="shell_employer_recent_residential",
                title="Recently registered employer at a residential address",
                explanation_en=(
                    f"The employer was registered approximately "
                    f"{months_before:.1f} months before the separation date "
                    f"(within the {window_months}-month shell-employer window) "
                    f"and its registered address is residential — a strong shell "
                    f"employer signal."
                ),
                explanation_fr=(
                    f"L'employeur a ete enregistre environ "
                    f"{months_before:.1f} mois avant la date de cessation "
                    f"d'emploi (dans la fenetre de {window_months} mois pour les "
                    f"employeurs fictifs) et son adresse enregistree est "
                    f"residentielle — un signal fort d'employeur fictif."
                ),
                payload={
                    "employer_bn": employer_bn,
                    "reason": "RECENT_REGISTRATION_RESIDENTIAL",
                    "registration_date": reg_date.isoformat(),
                    "separation_date": separation_date.isoformat(),
                    "months_before_separation": round(months_before, 2),
                    "window_months": window_months,
                    "address_type": _address_type_str(registration),
                },
            )
        ]

    # Any other case (older registration, or non-residential address) -> no flag.
    return []


# ---------------------------------------------------------------------------
# 2. Coordinated-layoff detection (Req 5.3, 5.4)
# ---------------------------------------------------------------------------


def _dominant_same_date_group(
    members: Sequence[CohortMember],
) -> tuple[Optional[date], list[CohortMember]]:
    """Return the separation date shared by the most members, and that group.

    Ties are broken by the earliest separation date for deterministic output.
    """
    if not members:
        return None, []

    groups: dict[date, list[CohortMember]] = {}
    for m in members:
        groups.setdefault(m.separation_date, []).append(m)

    # Largest group first; break ties by earliest date.
    best_date = max(groups, key=lambda d: (len(groups[d]), -d.toordinal()))
    return best_date, groups[best_date]


def _all_within_rolling_window(
    timestamps: Sequence[datetime], window_hours: int
) -> bool:
    """True iff all timestamps fit inside some rolling window of ``window_hours``.

    Because the maximum span between any two timestamps is (max - min), the whole
    set fits inside a single window exactly when (max - min) <= window. The
    "any 24-hour rolling window" phrasing is therefore equivalent to bounding the
    total spread. Naive datetimes are treated as UTC.
    """
    if len(timestamps) <= 1:
        return True

    def _as_utc(ts: datetime) -> datetime:
        return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)

    normalised = [_as_utc(ts) for ts in timestamps]
    span = max(normalised) - min(normalised)
    window_seconds = window_hours * 3600
    return span.total_seconds() <= window_seconds


def check_coordinated_layoff(
    cohort: Iterable[CohortMember],
    *,
    same_date_fraction: float = DEFAULT_COORDINATED_SAME_DATE_FRACTION,
    roe_window_hours: int = DEFAULT_COORDINATED_ROE_WINDOW_HOURS,
    claim_id: Optional[str] = None,
    employer_bn: Optional[str] = None,
) -> list[Indicator]:
    """Flag a coordinated mass layoff over the 90-day separation cohort.

    The cohort is expected to already be filtered to employees separated within
    the 90-day window before the claim's separation date (the caller / repository
    performs that filtering).

    A CRITICAL Indicator is produced when BOTH conditions hold (Req 5.3, 5.4):
      1. At least ``same_date_fraction`` (default 80%) of the cohort share the
         same separation calendar date. Boundary: 79% -> no flag, 80% -> flag.
      2. Every ROE submission timestamp in that same-date group falls within any
         rolling ``roe_window_hours`` (default 24-hour) window.

    Parameters
    ----------
    cohort:
        Iterable of ``CohortMember`` for the 90-day window.
    same_date_fraction:
        Minimum fraction of the cohort that must share one separation date.
    roe_window_hours:
        Width of the rolling ROE-submission window in hours.
    claim_id:
        Optional id of the claim under analysis, attached to the Indicator.
    employer_bn:
        Optional employer BN recorded on the Indicator payload for evidence.

    Returns
    -------
    list[Indicator]
        A single-element list with the CRITICAL Indicator when a coordinated
        layoff is detected; otherwise an empty list.
    """
    members = list(cohort)
    total = len(members)
    if total == 0:
        return []

    same_date, group = _dominant_same_date_group(members)
    group_size = len(group)
    fraction = group_size / total

    # ── Condition 1: >= same_date_fraction share the same separation date ──
    if fraction < same_date_fraction:
        return []

    # ── Condition 2: all ROEs of that group within a rolling window ──
    roe_timestamps = [m.roe_submission_ts for m in group]
    if not _all_within_rolling_window(roe_timestamps, roe_window_hours):
        return []

    ordered_ts = sorted(
        ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
        for ts in roe_timestamps
    )
    roe_span_hours = (
        (ordered_ts[-1] - ordered_ts[0]).total_seconds() / 3600.0
        if len(ordered_ts) > 1
        else 0.0
    )
    pct = round(fraction * 100.0, 2)

    return [
        _new_indicator(
            claim_id=claim_id,
            severity=Severity.CRITICAL,
            algorithm="coordinated_layoff",
            title=(
                f"Coordinated layoff: {group_size}/{total} separated "
                f"{same_date.isoformat()}"
            ),
            explanation_en=(
                f"{group_size} of {total} employees ({pct:.0f}%) separated on the "
                f"same date ({same_date.isoformat()}), and all of their Records of "
                f"Employment were submitted within a {roe_window_hours}-hour "
                f"window (span {roe_span_hours:.1f}h) — consistent with a "
                f"coordinated mass layoff."
            ),
            explanation_fr=(
                f"{group_size} des {total} employes ({pct:.0f}%) ont cesse leur "
                f"emploi a la meme date ({same_date.isoformat()}), et tous leurs "
                f"releves d'emploi ont ete soumis dans une fenetre de "
                f"{roe_window_hours} heures (intervalle de {roe_span_hours:.1f}h) — "
                f"ce qui correspond a un licenciement collectif coordonne."
            ),
            payload={
                "employer_bn": employer_bn,
                "reason": "COORDINATED_LAYOFF",
                "cohort_size": total,
                "same_date_count": group_size,
                "same_date_fraction": round(fraction, 4),
                "same_date_fraction_threshold": same_date_fraction,
                "shared_separation_date": same_date.isoformat(),
                "roe_window_hours": roe_window_hours,
                "roe_span_hours": round(roe_span_hours, 2),
            },
        )
    ]

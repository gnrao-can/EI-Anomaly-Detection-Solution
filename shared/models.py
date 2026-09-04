"""
shared/models.py
================
Canonical data models for the AI-Enabled Employment Insurance Anomaly Detection Solution
(EI-ADS).

All domain objects are expressed as Python dataclasses with full type annotations.
Each dataclass implements:
  - ``__eq__`` and ``__hash__`` so instances can be compared and used in sets / dict keys.
  - ``to_dict()``  — converts the instance to a plain ``dict`` suitable for JSON serialisation.
  - ``from_dict()`` — class-method constructor that restores an instance from such a ``dict``.

Enumerations follow the values documented in the design and tasks specification.

Requirements: 1.4, 2.6, 9.1
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional
from uuid import UUID


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ClaimStatus(str, Enum):
    """Lifecycle states of a single EI claim as it moves through EI-ADS."""

    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    SCORED = "SCORED"
    INVESTIGATE = "INVESTIGATE"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class EngineType(str, Enum):
    """Identifies which Detection_Engine produced a given Indicator."""

    REPETITIVE_PATTERN = "REPETITIVE_PATTERN"
    GEOGRAPHIC_ANOMALY = "GEOGRAPHIC_ANOMALY"
    FRAUD_RING = "FRAUD_RING"
    HISTORY_INCONSISTENCY = "HISTORY_INCONSISTENCY"
    DOCUMENT_FORGERY = "DOCUMENT_FORGERY"
    REGIONAL_ANALYSIS = "REGIONAL_ANALYSIS"


class Severity(str, Enum):
    """Risk severity levels assigned to Indicators by Detection_Engines."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    UNRESOLVED = "UNRESOLVED"


class RiskDecision(str, Enum):
    """Routing outcome assigned to a claim after Ensemble_Scorer aggregation."""

    AUTO_APPROVE = "AUTO_APPROVE"
    FLAG_FOR_REVIEW = "FLAG_FOR_REVIEW"
    INVESTIGATE = "INVESTIGATE"
    INDETERMINATE = "INDETERMINATE"


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _uuid_to_str(value: UUID | str | None) -> str | None:
    """Serialise a UUID (or str) to a canonical string; pass ``None`` through."""
    if value is None:
        return None
    return str(value)


def _str_to_uuid(value: str | UUID | None) -> UUID | None:
    """Deserialise a string to a UUID; pass through existing UUIDs and ``None``."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    return UUID(value)


def _decimal_to_str(value: Decimal | None) -> str | None:
    """Serialise a Decimal to its exact string representation (no float rounding)."""
    if value is None:
        return None
    return str(value)


def _str_to_decimal(value: str | int | float | Decimal | None) -> Decimal | None:
    """Deserialise a value to ``Decimal`` using the string path to avoid float loss."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# Dataclass: Claim
# ---------------------------------------------------------------------------


@dataclass
class Claim:
    """
    Canonical model for an Employment Insurance claim record.

    The raw SIN is *never* stored; only its SHA-256 digest (``sin_hash``) is
    persisted, satisfying Requirement 1.11 (Protected B / SIN handling).

    Attributes
    ----------
    claim_id:
        Unique claim identifier (UUID).
    sin_hash:
        SHA-256 hex digest of the claimant's SIN (salted per environment).
    employer_bn:
        15-character employer Business Number (9-digit BN + RT + 4-digit account).
    insurable_hours:
        Total insurable hours worked; stored as ``Decimal(10, 2)``.
    insurable_earnings:
        Total insurable earnings in CAD; stored as ``Decimal(12, 2)``.
    separation_date:
        Date on which employment ended.
    separation_reason:
        Free-text reason for separation (max 1 000 chars).
    separation_reason_code:
        ROE reason code character (A–N).
    claim_start_date:
        First day of the EI benefit period.
    economic_region_code:
        ESDC 2-character economic region code.
    noc_code:
        NOC 2021 5-digit occupational classification code.
    residential_postal_code:
        First 3 characters of the claimant's postal code (FSA only).
    residential_address_hash:
        SHA-256 hex digest of the claimant's full residential address.
    employer_work_location:
        Full declared work location address for the employer.
    received_at:
        UTC timestamp of when the claim was received by the Claim_Intake_Service.
    status:
        Current lifecycle status of the claim.
    roe_document_id:
        UUID of the associated ROE document, if one has been matched.
    protected_b:
        Always ``True``; records that this data is classified Protected B.
    """

    claim_id: UUID
    sin_hash: str
    employer_bn: str
    insurable_hours: Decimal
    insurable_earnings: Decimal
    separation_date: date
    separation_reason: str
    separation_reason_code: str
    claim_start_date: date
    economic_region_code: str
    noc_code: str
    residential_postal_code: str
    residential_address_hash: str
    employer_work_location: str
    received_at: datetime
    status: ClaimStatus
    roe_document_id: Optional[UUID] = None
    protected_b: bool = True

    # ------------------------------------------------------------------
    # Equality and hashing
    # ------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Claim):
            return NotImplemented
        return self.claim_id == other.claim_id

    def __hash__(self) -> int:
        return hash(self.claim_id)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable ``dict`` representation of this Claim."""
        return {
            "claim_id": _uuid_to_str(self.claim_id),
            "sin_hash": self.sin_hash,
            "employer_bn": self.employer_bn,
            "insurable_hours": _decimal_to_str(self.insurable_hours),
            "insurable_earnings": _decimal_to_str(self.insurable_earnings),
            "separation_date": self.separation_date.isoformat(),
            "separation_reason": self.separation_reason,
            "separation_reason_code": self.separation_reason_code,
            "claim_start_date": self.claim_start_date.isoformat(),
            "economic_region_code": self.economic_region_code,
            "noc_code": self.noc_code,
            "residential_postal_code": self.residential_postal_code,
            "residential_address_hash": self.residential_address_hash,
            "employer_work_location": self.employer_work_location,
            "received_at": self.received_at.isoformat(),
            "status": self.status.value,
            "roe_document_id": _uuid_to_str(self.roe_document_id),
            "protected_b": self.protected_b,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Claim:
        """Restore a ``Claim`` instance from a ``dict`` produced by ``to_dict()``."""
        return cls(
            claim_id=UUID(data["claim_id"]),
            sin_hash=data["sin_hash"],
            employer_bn=data["employer_bn"],
            insurable_hours=Decimal(data["insurable_hours"]),
            insurable_earnings=Decimal(data["insurable_earnings"]),
            separation_date=date.fromisoformat(data["separation_date"]),
            separation_reason=data["separation_reason"],
            separation_reason_code=data["separation_reason_code"],
            claim_start_date=date.fromisoformat(data["claim_start_date"]),
            economic_region_code=data["economic_region_code"],
            noc_code=data["noc_code"],
            residential_postal_code=data["residential_postal_code"],
            residential_address_hash=data["residential_address_hash"],
            employer_work_location=data["employer_work_location"],
            received_at=datetime.fromisoformat(data["received_at"]),
            status=ClaimStatus(data["status"]),
            roe_document_id=_str_to_uuid(data.get("roe_document_id")),
            protected_b=data.get("protected_b", True),
        )


# ---------------------------------------------------------------------------
# Dataclass: Indicator
# ---------------------------------------------------------------------------


@dataclass
class Indicator:
    """
    Structured anomaly signal produced by a Detection_Engine for a single claim.

    Each Indicator captures one anomalous observation, its severity, the
    algorithm that produced it, and bilingual natural-language explanations.

    Attributes
    ----------
    indicator_id:
        Unique identifier for this Indicator (UUID).
    claim_id:
        UUID of the claim this Indicator belongs to.
    engine:
        Which Detection_Engine produced this Indicator.
    severity:
        Risk severity level assigned to this observation.
    algorithm:
        Name of the specific algorithm that produced this Indicator
        (e.g. ``"sequential_pattern_mining"``, ``"haversine_distance"``).
    title:
        Short description of the anomaly (≤ 100 characters).
    explanation_en:
        Natural-language explanation in English (≤ 500 characters).
    explanation_fr:
        Equivalent explanation in French (≤ 500 characters).
    payload:
        Structured evidence dict (amounts, dates, identifiers) as free-form JSON.
    produced_at:
        UTC timestamp of when this Indicator was produced.
    shap_contribution:
        SHAP feature attribution contribution value assigned by the
        Ensemble_Scorer; ``None`` until scoring is complete.
    """

    indicator_id: UUID
    claim_id: UUID
    engine: EngineType
    severity: Severity
    algorithm: str
    title: str
    explanation_en: str
    explanation_fr: str
    payload: dict[str, Any]
    produced_at: datetime
    shap_contribution: Optional[float] = None

    # ------------------------------------------------------------------
    # Equality and hashing
    # ------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Indicator):
            return NotImplemented
        return self.indicator_id == other.indicator_id

    def __hash__(self) -> int:
        return hash(self.indicator_id)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable ``dict`` representation of this Indicator."""
        return {
            "indicator_id": _uuid_to_str(self.indicator_id),
            "claim_id": _uuid_to_str(self.claim_id),
            "engine": self.engine.value,
            "severity": self.severity.value,
            "algorithm": self.algorithm,
            "title": self.title,
            "explanation_en": self.explanation_en,
            "explanation_fr": self.explanation_fr,
            "payload": self.payload,
            "produced_at": self.produced_at.isoformat(),
            "shap_contribution": self.shap_contribution,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Indicator:
        """Restore an ``Indicator`` instance from a ``dict`` produced by ``to_dict()``."""
        return cls(
            indicator_id=UUID(data["indicator_id"]),
            claim_id=UUID(data["claim_id"]),
            engine=EngineType(data["engine"]),
            severity=Severity(data["severity"]),
            algorithm=data["algorithm"],
            title=data["title"],
            explanation_en=data["explanation_en"],
            explanation_fr=data["explanation_fr"],
            payload=data["payload"],
            produced_at=datetime.fromisoformat(data["produced_at"]),
            shap_contribution=data.get("shap_contribution"),
        )


# ---------------------------------------------------------------------------
# Dataclass: ShapAttribution
# ---------------------------------------------------------------------------


@dataclass
class ShapAttribution:
    """
    SHAP feature attribution record linking a contribution value to one Indicator.

    The additivity invariant (P7) requires that the sum of all
    ``contribution`` values for a single ``RiskAssessment`` equals
    ``risk_score - base_value`` within a floating-point tolerance of 1 × 10⁻⁴.

    Attributes
    ----------
    indicator_id:
        UUID of the Indicator this attribution is associated with.
    feature_name:
        Human-readable feature name used by the XGBoost model
        (e.g. ``"repetitive_pattern_high_severity_count"``).
    contribution:
        Signed SHAP contribution value; positive values increase the
        Risk_Score and negative values decrease it.
    base_value:
        Model base value (``explainer.expected_value``) — the mean
        prediction over the training set.
    """

    indicator_id: UUID
    feature_name: str
    contribution: float
    base_value: float

    # ------------------------------------------------------------------
    # Equality and hashing
    # ------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ShapAttribution):
            return NotImplemented
        return (
            self.indicator_id == other.indicator_id
            and self.feature_name == other.feature_name
        )

    def __hash__(self) -> int:
        return hash((self.indicator_id, self.feature_name))

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable ``dict`` representation."""
        return {
            "indicator_id": _uuid_to_str(self.indicator_id),
            "feature_name": self.feature_name,
            "contribution": self.contribution,
            "base_value": self.base_value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ShapAttribution:
        """Restore a ``ShapAttribution`` from a ``dict`` produced by ``to_dict()``."""
        return cls(
            indicator_id=UUID(data["indicator_id"]),
            feature_name=data["feature_name"],
            contribution=float(data["contribution"]),
            base_value=float(data["base_value"]),
        )


# ---------------------------------------------------------------------------
# Dataclass: RiskAssessment
# ---------------------------------------------------------------------------


@dataclass
class RiskAssessment:
    """
    Aggregated risk assessment produced by the Ensemble_Scorer for one claim.

    Combines the XGBoost + Isolation Forest Risk_Score, the SHAP attribution
    vector, and bilingual LLM-generated explanations.

    Attributes
    ----------
    assessment_id:
        Unique identifier for this assessment (UUID).
    claim_id:
        UUID of the claim this assessment belongs to.
    risk_score:
        Integer risk score in [0, 100].
    risk_decision:
        Routing decision derived from the risk score thresholds.
    shap_base_value:
        Model base value (``explainer.expected_value``).
    shap_attributions:
        Per-Indicator SHAP attribution records.  The additivity invariant
        (P7) must hold: ``|sum(a.contribution) - (risk_score - shap_base_value)| < 1e-4``.
    explanation_en:
        LLM-generated risk explanation in English (≤ 500 characters).
    explanation_fr:
        LLM-generated risk explanation in French (≤ 500 characters).
    engines_completed:
        List of EngineTypes that returned results within their time budget.
    engines_partial:
        List of EngineTypes that did not complete within their time budget.
    scored_at:
        UTC timestamp of when scoring was completed.
    model_version:
        Identifier of the XGBoost/Isolation Forest model artefact used.
    """

    assessment_id: UUID
    claim_id: UUID
    risk_score: int
    risk_decision: RiskDecision
    shap_base_value: float
    shap_attributions: list[ShapAttribution]
    explanation_en: str
    explanation_fr: str
    engines_completed: list[EngineType]
    engines_partial: list[EngineType]
    scored_at: datetime
    model_version: str

    # ------------------------------------------------------------------
    # Equality and hashing
    # ------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RiskAssessment):
            return NotImplemented
        return self.assessment_id == other.assessment_id

    def __hash__(self) -> int:
        return hash(self.assessment_id)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable ``dict`` representation."""
        return {
            "assessment_id": _uuid_to_str(self.assessment_id),
            "claim_id": _uuid_to_str(self.claim_id),
            "risk_score": self.risk_score,
            "risk_decision": self.risk_decision.value,
            "shap_base_value": self.shap_base_value,
            "shap_attributions": [a.to_dict() for a in self.shap_attributions],
            "explanation_en": self.explanation_en,
            "explanation_fr": self.explanation_fr,
            "engines_completed": [e.value for e in self.engines_completed],
            "engines_partial": [e.value for e in self.engines_partial],
            "scored_at": self.scored_at.isoformat(),
            "model_version": self.model_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RiskAssessment:
        """Restore a ``RiskAssessment`` from a ``dict`` produced by ``to_dict()``."""
        return cls(
            assessment_id=UUID(data["assessment_id"]),
            claim_id=UUID(data["claim_id"]),
            risk_score=int(data["risk_score"]),
            risk_decision=RiskDecision(data["risk_decision"]),
            shap_base_value=float(data["shap_base_value"]),
            shap_attributions=[
                ShapAttribution.from_dict(a) for a in data.get("shap_attributions", [])
            ],
            explanation_en=data.get("explanation_en", ""),
            explanation_fr=data.get("explanation_fr", ""),
            engines_completed=[EngineType(e) for e in data.get("engines_completed", [])],
            engines_partial=[EngineType(e) for e in data.get("engines_partial", [])],
            scored_at=datetime.fromisoformat(data["scored_at"]),
            model_version=data["model_version"],
        )


# ---------------------------------------------------------------------------
# Dataclass: LabourForceData
# ---------------------------------------------------------------------------


@dataclass
class LabourForceData:
    """
    Regional labour force statistics from the Statistics Canada LFS API.

    Parsed and cached by the Stats_Canada_Adapter; consumed by the
    Regional_Analysis_Engine.  All rate fields retain a minimum of 4 decimal
    places via ``Decimal(str(value))`` parsing (Requirement 10.6, P10).

    Attributes
    ----------
    reference_month:
        First day of the reference calendar month for this data point.
    esdc_region_code:
        ESDC 2-character economic region code.
    region_name_en:
        English name of the ESDC economic region.
    region_name_fr:
        French name of the ESDC economic region.
    unemployment_rate:
        Regional unemployment rate; at least 4 decimal places of precision.
    employment_rate:
        Regional employment rate; at least 4 decimal places of precision.
    participation_rate:
        Regional labour force participation rate; at least 4 decimal places.
    national_unemployment_rate:
        National average unemployment rate for the same reference month.
    source_api_version:
        Version string of the Stats Canada API that provided this data.
    retrieved_at:
        UTC timestamp of when this record was fetched and cached.
    data_stale:
        ``True`` when the record was served from a cache older than 30 days
        (``DATA_STALE`` flag per Requirement 10.2).
    """

    reference_month: date
    esdc_region_code: str
    region_name_en: str
    region_name_fr: str
    unemployment_rate: Decimal
    employment_rate: Decimal
    participation_rate: Decimal
    national_unemployment_rate: Decimal
    source_api_version: str
    retrieved_at: datetime
    data_stale: bool = False

    # ------------------------------------------------------------------
    # Equality and hashing
    # ------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LabourForceData):
            return NotImplemented
        return (
            self.esdc_region_code == other.esdc_region_code
            and self.reference_month == other.reference_month
        )

    def __hash__(self) -> int:
        return hash((self.esdc_region_code, self.reference_month))

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable ``dict`` representation.

        Decimal fields are serialised as strings to preserve exact precision
        through JSON round-trips (satisfying P10).
        """
        return {
            "reference_month": self.reference_month.isoformat(),
            "esdc_region_code": self.esdc_region_code,
            "region_name_en": self.region_name_en,
            "region_name_fr": self.region_name_fr,
            "unemployment_rate": _decimal_to_str(self.unemployment_rate),
            "employment_rate": _decimal_to_str(self.employment_rate),
            "participation_rate": _decimal_to_str(self.participation_rate),
            "national_unemployment_rate": _decimal_to_str(self.national_unemployment_rate),
            "source_api_version": self.source_api_version,
            "retrieved_at": self.retrieved_at.isoformat(),
            "data_stale": self.data_stale,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LabourForceData:
        """Restore a ``LabourForceData`` from a ``dict`` produced by ``to_dict()``."""
        return cls(
            reference_month=date.fromisoformat(data["reference_month"]),
            esdc_region_code=data["esdc_region_code"],
            region_name_en=data["region_name_en"],
            region_name_fr=data["region_name_fr"],
            unemployment_rate=Decimal(data["unemployment_rate"]),
            employment_rate=Decimal(data["employment_rate"]),
            participation_rate=Decimal(data["participation_rate"]),
            national_unemployment_rate=Decimal(data["national_unemployment_rate"]),
            source_api_version=data["source_api_version"],
            retrieved_at=datetime.fromisoformat(data["retrieved_at"]),
            data_stale=data.get("data_stale", False),
        )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    # Enumerations
    "ClaimStatus",
    "EngineType",
    "Severity",
    "RiskDecision",
    # Dataclasses
    "Claim",
    "Indicator",
    "ShapAttribution",
    "RiskAssessment",
    "LabourForceData",
]

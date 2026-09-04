"""
shared — EI-ADS canonical data models, enumerations, and utility helpers.

This package is installed as an editable dependency across all EI-ADS services.
Import the canonical models from `shared.models`; cache helpers live in
`shared.cache` and `shared.cache_keys`.
"""

from shared.models import (
    ClaimStatus,
    EngineType,
    Severity,
    RiskDecision,
    Claim,
    Indicator,
    RiskAssessment,
    ShapAttribution,
    LabourForceData,
)

__all__ = [
    "ClaimStatus",
    "EngineType",
    "Severity",
    "RiskDecision",
    "Claim",
    "Indicator",
    "RiskAssessment",
    "ShapAttribution",
    "LabourForceData",
]

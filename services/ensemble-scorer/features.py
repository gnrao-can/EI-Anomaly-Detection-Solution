"""
services/ensemble-scorer/features.py
=====================================
Feature engineering for the Ensemble Scorer.

The :class:`FeatureEncoder` converts a list of :class:`~shared.models.Indicator`
objects into the fixed-length numeric feature vector consumed by both the
XGBoost classifier and the Isolation Forest anomaly detector.

Design (see design.md — "Ensemble Scorer Design"):
  - One-hot encoded engine types (presence flag per EngineType)
  - Per-engine Indicator counts
  - Ordinal severity encoding (LOW=1, MEDIUM=2, HIGH=3, CRITICAL=4; UNRESOLVED=0)
  - Aggregate severity signals (sum and max ordinal severity)
  - Per-engine "completion" flags derived from ``engines_completed``
  - Total Indicator count

The layout is deterministic: the ordering of ``EngineType`` and ``Severity``
members is fixed at import time, so the same Indicator list always produces the
same vector, and the vector length is constant regardless of input.

Requirements: 9.1, 9.2
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

try:  # NumPy is part of the ML stack and is intentionally absent in the local
    # core-service dev environment. Guard the import so this module (and the
    # EnsembleScorer that depends on it) still loads and constructs in heuristic
    # fallback mode; the numeric encode paths that use ``np`` only run when the
    # full env (container/CI) has NumPy installed.
    import numpy as np

    _HAS_NUMPY = True
except ImportError:  # pragma: no cover - NumPy absent in minimal environments
    np = None  # type: ignore[assignment]
    _HAS_NUMPY = False

try:  # Allow import when run from the service directory or as a package.
    from shared.models import EngineType, Indicator, Severity
except ImportError:  # pragma: no cover - fallback for unusual sys.path setups
    from models import EngineType, Indicator, Severity  # type: ignore


# ---------------------------------------------------------------------------
# Fixed, deterministic orderings
# ---------------------------------------------------------------------------

# The six canonical detection engines, in a fixed order. This ordering defines
# the column layout of the one-hot / count / completion blocks of the feature
# vector and MUST remain stable across training and inference.
ENGINE_ORDER: tuple[EngineType, ...] = (
    EngineType.REPETITIVE_PATTERN,
    EngineType.GEOGRAPHIC_ANOMALY,
    EngineType.FRAUD_RING,
    EngineType.HISTORY_INCONSISTENCY,
    EngineType.DOCUMENT_FORGERY,
    EngineType.REGIONAL_ANALYSIS,
)

# Ordinal severity encoding. UNRESOLVED contributes no risk weight (0); the
# graded levels increase from LOW (1) to CRITICAL (4).
SEVERITY_ORDINAL: dict[Severity, int] = {
    Severity.UNRESOLVED: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}

_NUM_ENGINES = len(ENGINE_ORDER)

# Feature vector layout (all blocks concatenated in this order):
#   [0 .. E)              one-hot engine presence flags        (E entries)
#   [E .. 2E)             per-engine indicator counts          (E entries)
#   [2E .. 3E)            per-engine completion flags          (E entries)
#   [3E + 0]             total indicator count
#   [3E + 1]             sum of ordinal severities
#   [3E + 2]             max ordinal severity
#   [3E + 3]             count of HIGH-or-CRITICAL indicators
FEATURE_VECTOR_LENGTH = _NUM_ENGINES * 3 + 4

# Human-readable feature names, aligned 1:1 with the vector columns. Useful for
# SHAP attribution labelling in task 7.2.
FEATURE_NAMES: tuple[str, ...] = (
    tuple(f"{e.value.lower()}_present" for e in ENGINE_ORDER)
    + tuple(f"{e.value.lower()}_count" for e in ENGINE_ORDER)
    + tuple(f"{e.value.lower()}_completed" for e in ENGINE_ORDER)
    + ("total_indicator_count", "severity_ordinal_sum", "severity_ordinal_max",
       "high_or_critical_count")
)

assert len(FEATURE_NAMES) == FEATURE_VECTOR_LENGTH  # invariant guard


class FeatureEncoder:
    """Encodes an ``Indicator`` list into the ensemble model feature vector.

    The encoder is stateless and deterministic. Instances are cheap to create;
    a module-level singleton is also exposed as :data:`DEFAULT_ENCODER`.
    """

    #: Length of every vector produced by this encoder.
    vector_length: int = FEATURE_VECTOR_LENGTH
    #: Ordered feature names aligned with the vector columns.
    feature_names: tuple[str, ...] = FEATURE_NAMES

    def encode(
        self,
        indicators: Sequence[Indicator],
        engines_completed: Optional[Iterable[EngineType]] = None,
    ) -> np.ndarray:
        """Convert an Indicator list to a 1-D float feature vector.

        Parameters
        ----------
        indicators:
            The Indicators produced for a single claim. May be empty.
        engines_completed:
            Optional iterable of ``EngineType`` values that completed within
            their time budget. When omitted, an engine is considered "completed"
            if it produced at least one Indicator.

        Returns
        -------
        numpy.ndarray
            A deterministic float64 vector of length
            :data:`FEATURE_VECTOR_LENGTH`.
        """
        vec = np.zeros(FEATURE_VECTOR_LENGTH, dtype=np.float64)

        engine_index = {engine: i for i, engine in enumerate(ENGINE_ORDER)}

        severity_sum = 0
        severity_max = 0
        high_or_critical = 0

        for indicator in indicators:
            idx = engine_index.get(indicator.engine)
            if idx is None:
                # Unknown engine type: skip its one-hot/count contribution but
                # still fold its severity into the aggregate signals.
                pass
            else:
                vec[idx] = 1.0  # presence flag
                vec[_NUM_ENGINES + idx] += 1.0  # per-engine count

            ordinal = SEVERITY_ORDINAL.get(indicator.severity, 0)
            severity_sum += ordinal
            severity_max = max(severity_max, ordinal)
            if ordinal >= SEVERITY_ORDINAL[Severity.HIGH]:
                high_or_critical += 1

        # Completion flags block.
        completed_set: set[EngineType]
        if engines_completed is not None:
            completed_set = set(engines_completed)
        else:
            completed_set = {ind.engine for ind in indicators}
        for engine, idx in engine_index.items():
            if engine in completed_set:
                vec[2 * _NUM_ENGINES + idx] = 1.0

        base = 3 * _NUM_ENGINES
        vec[base + 0] = float(len(indicators))
        vec[base + 1] = float(severity_sum)
        vec[base + 2] = float(severity_max)
        vec[base + 3] = float(high_or_critical)

        return vec

    def encode_batch(
        self,
        indicator_lists: Sequence[Sequence[Indicator]],
    ) -> np.ndarray:
        """Encode several Indicator lists into a 2-D matrix (rows = claims)."""
        if not indicator_lists:
            return np.zeros((0, FEATURE_VECTOR_LENGTH), dtype=np.float64)
        return np.vstack([self.encode(lst) for lst in indicator_lists])


#: Shared stateless encoder instance for convenient reuse.
DEFAULT_ENCODER = FeatureEncoder()


__all__ = [
    "FeatureEncoder",
    "DEFAULT_ENCODER",
    "ENGINE_ORDER",
    "SEVERITY_ORDINAL",
    "FEATURE_VECTOR_LENGTH",
    "FEATURE_NAMES",
]

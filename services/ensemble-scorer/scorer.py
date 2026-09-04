"""
services/ensemble-scorer/scorer.py
===================================
The :class:`EnsembleScorer` combines an XGBoost classifier and an Isolation
Forest anomaly detector into a single integer Risk_Score in ``[0, 100]`` and a
:class:`~shared.models.RiskDecision`.

Scoring formula (design.md — "Ensemble Scorer Design"):

    xgb_prob   in [0, 1]                      (XGBoost probability)
    if_score   in [0, 1]                      (normalised Isolation Forest anomaly)
    final      = min(100, round(xgb_prob * 70 + if_score * 30))

Isolation Forest contributes an anomaly *bonus* in ``[0, 30]`` (that is,
``if_score * 30``), matching the design's "bonus score" language.

Decision thresholds:

    [0, 29]   -> AUTO_APPROVE
    [30, 69]  -> FLAG_FOR_REVIEW
    [70, 100] -> INVESTIGATE

Model loading:
  - Pre-trained models are loaded from ``models/xgboost_v1.joblib`` and
    ``models/isolation_forest_v1.joblib`` via :mod:`joblib`.
  - If the artefacts (or their libraries) are unavailable, the scorer falls
    back to a deterministic, documented heuristic so the service still runs in
    development. The heuristic is monotonic in the Indicator set (adding
    Indicators never lowers the score), preserving the design's monotonicity
    guarantee (P9), and returns 0 / AUTO_APPROVE for an empty Indicator list
    (P8).

Requirements: 9.1, 9.2
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

try:  # NumPy is part of the ML stack and is intentionally absent in the local
    # core-service dev environment. Guard the import so the module loads and the
    # EnsembleScorer constructs in heuristic fallback mode without it; the
    # numeric scoring paths that use ``np`` only run when the full env has NumPy.
    import numpy as np

    _HAS_NUMPY = True
except ImportError:  # pragma: no cover - NumPy absent in minimal environments
    np = None  # type: ignore[assignment]
    _HAS_NUMPY = False

try:  # package/relative import tolerance
    from shared.models import (
        EngineType,
        Indicator,
        RiskAssessment,
        RiskDecision,
        Severity,
        ShapAttribution,
    )
except ImportError:  # pragma: no cover
    from models import (  # type: ignore
        EngineType,
        Indicator,
        RiskAssessment,
        RiskDecision,
        Severity,
        ShapAttribution,
    )

try:
    from features import DEFAULT_ENCODER, SEVERITY_ORDINAL, FeatureEncoder
except ImportError:  # pragma: no cover
    from .features import DEFAULT_ENCODER, SEVERITY_ORDINAL, FeatureEncoder  # type: ignore

try:
    from shap_explainer import ShapAttributor, assert_additivity
except ImportError:  # pragma: no cover
    from .shap_explainer import ShapAttributor, assert_additivity  # type: ignore


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

XGB_WEIGHT = 70.0
IF_WEIGHT = 30.0

AUTO_APPROVE_MAX = 29
FLAG_MAX = 69
# 70..100 -> INVESTIGATE

_DEFAULT_MODEL_DIR = Path(__file__).resolve().parent / "models"
XGB_MODEL_FILENAME = "xgboost_v1.joblib"
IF_MODEL_FILENAME = "isolation_forest_v1.joblib"

MODEL_VERSION_TRAINED = "ensemble_v1"
MODEL_VERSION_HEURISTIC = "heuristic_fallback_v1"


def decision_for_score(score: int) -> RiskDecision:
    """Map an integer Risk_Score in ``[0, 100]`` to a :class:`RiskDecision`.

    Boundaries (inclusive lower bounds) per design.md:
      - ``[0, 29]``  -> AUTO_APPROVE
      - ``[30, 69]`` -> FLAG_FOR_REVIEW
      - ``[70, 100]`` -> INVESTIGATE
    """
    if score <= AUTO_APPROVE_MAX:
        return RiskDecision.AUTO_APPROVE
    if score <= FLAG_MAX:
        return RiskDecision.FLAG_FOR_REVIEW
    return RiskDecision.INVESTIGATE


class EnsembleScorer:
    """Score claims from their Indicator sets using XGBoost + Isolation Forest.

    Parameters
    ----------
    model_dir:
        Directory containing ``xgboost_v1.joblib`` and
        ``isolation_forest_v1.joblib``. Defaults to ``<service>/models``.
    encoder:
        Feature encoder to use. Defaults to the shared stateless encoder.

    Notes
    -----
    When the pre-trained artefacts cannot be loaded the scorer transparently
    uses a deterministic heuristic; :attr:`using_fallback` reports which mode is
    active and :attr:`model_version` reflects the effective model identifier.
    """

    def __init__(
        self,
        model_dir: Optional[os.PathLike[str] | str] = None,
        encoder: Optional[FeatureEncoder] = None,
    ) -> None:
        self.model_dir = Path(model_dir) if model_dir is not None else _DEFAULT_MODEL_DIR
        self.encoder = encoder or DEFAULT_ENCODER
        self._xgb = None
        self._iforest = None
        self.using_fallback = True
        self.model_version = MODEL_VERSION_HEURISTIC
        self._load_models()
        # SHAP attributor wraps the loaded XGBoost model when available; it
        # transparently uses a deterministic linear fallback otherwise. Built
        # after model loading so it can pick up the real model / expected_value.
        self.attributor = ShapAttributor(
            xgboost_model=self._xgb, encoder=self.encoder
        )

    # ------------------------------------------------------------------
    # Model loading
    # ------------------------------------------------------------------

    def _load_models(self) -> None:
        """Attempt to load both artefacts; fall back to heuristic on any error."""
        xgb_path = self.model_dir / XGB_MODEL_FILENAME
        if_path = self.model_dir / IF_MODEL_FILENAME
        if not (xgb_path.exists() and if_path.exists()):
            self.using_fallback = True
            self.model_version = MODEL_VERSION_HEURISTIC
            return
        try:
            import joblib  # local import so the module imports without joblib

            self._xgb = joblib.load(xgb_path)
            self._iforest = joblib.load(if_path)
            self.using_fallback = False
            self.model_version = MODEL_VERSION_TRAINED
        except Exception:  # noqa: BLE001 - any failure => heuristic mode
            self._xgb = None
            self._iforest = None
            self.using_fallback = True
            self.model_version = MODEL_VERSION_HEURISTIC

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score(self, indicators: Sequence[Indicator]) -> tuple[int, RiskDecision]:
        """Compute the integer Risk_Score and RiskDecision for an Indicator set.

        An empty Indicator list always yields ``(0, AUTO_APPROVE)`` (P8),
        regardless of model mode.
        """
        if not indicators:
            return 0, RiskDecision.AUTO_APPROVE

        features = self.encoder.encode(indicators)

        if self.using_fallback or self._xgb is None or self._iforest is None:
            xgb_prob, if_score = self._heuristic_components(indicators, features)
        else:
            xgb_prob = self._xgb_probability(features)
            if_score = self._iforest_score(features)

        raw = xgb_prob * XGB_WEIGHT + if_score * IF_WEIGHT
        final = min(100, int(round(raw)))
        final = max(0, final)
        return final, decision_for_score(final)

    def assess(
        self,
        indicators: Sequence[Indicator],
        claim_id: Optional[uuid.UUID] = None,
        engines_completed: Optional[Sequence[EngineType]] = None,
        engines_partial: Optional[Sequence[EngineType]] = None,
    ) -> RiskAssessment:
        """Score a claim and build a full :class:`RiskAssessment`.

        This is the task-7.2 scoring flow: compute the Risk_Score, produce the
        per-Indicator SHAP attribution list, assert the additivity invariant
        (P7), and assemble a :class:`RiskAssessment`. The bilingual
        ``explanation_en`` / ``explanation_fr`` fields are intentionally left
        empty — they are filled in later by the LLM service.

        Parameters
        ----------
        indicators:
            The Indicators produced for the claim. May be empty (yields a
            zero-score AUTO_APPROVE assessment with no attributions, P8).
        claim_id:
            The claim's UUID. When omitted, it is taken from the first Indicator
            (all Indicators for a claim share the same ``claim_id``), or a fresh
            UUID is generated if there are no Indicators.
        engines_completed / engines_partial:
            Optional engine-completion bookkeeping recorded on the assessment.
            When ``engines_completed`` is omitted it is derived from the set of
            engines that produced Indicators.
        """
        risk_score, risk_decision = self.score(indicators)

        feature_vector = self.encoder.encode(indicators)
        attributions, base_value = self.attributor.explain(
            feature_vector, indicators, risk_score
        )

        # Defensive re-check of the additivity invariant (P7) at the scorer
        # boundary, independent of the attributor's internal assertion.
        assert_additivity(
            [a.contribution for a in attributions], risk_score, base_value
        )

        resolved_claim_id = claim_id
        if resolved_claim_id is None:
            resolved_claim_id = (
                indicators[0].claim_id if indicators else uuid.uuid4()
            )

        if engines_completed is not None:
            completed = list(engines_completed)
        else:
            completed = list({ind.engine for ind in indicators})

        return RiskAssessment(
            assessment_id=uuid.uuid4(),
            claim_id=resolved_claim_id,
            risk_score=risk_score,
            risk_decision=risk_decision,
            shap_base_value=base_value,
            shap_attributions=attributions,
            explanation_en="",  # filled by LLM service later
            explanation_fr="",  # filled by LLM service later
            engines_completed=completed,
            engines_partial=list(engines_partial) if engines_partial else [],
            scored_at=datetime.now(timezone.utc),
            model_version=self.model_version,
        )

    # ------------------------------------------------------------------
    # Trained-model component helpers
    # ------------------------------------------------------------------

    def _xgb_probability(self, features: np.ndarray) -> float:
        """Return the XGBoost fraud probability in ``[0, 1]``."""
        X = features.reshape(1, -1)
        model = self._xgb
        if hasattr(model, "predict_proba"):
            proba = model.predict_proba(X)
            # Binary classifier: probability of the positive (fraud) class.
            p = float(proba[0][-1])
        else:  # Booster or regressor
            p = float(np.ravel(model.predict(X))[0])
        return float(min(1.0, max(0.0, p)))

    def _iforest_score(self, features: np.ndarray) -> float:
        """Return the Isolation Forest anomaly score normalised to ``[0, 1]``.

        ``IsolationForest.score_samples`` returns higher values for normal
        points and lower (more negative) values for anomalies. We invert and
        squash into ``[0, 1]`` where 1 means "most anomalous".
        """
        X = features.reshape(1, -1)
        model = self._iforest
        # decision_function: positive => inlier, negative => outlier.
        raw = float(model.decision_function(X)[0])
        # Map to [0, 1] anomaly-ness: sigmoid on the negated score.
        anomaly = 1.0 / (1.0 + np.exp(raw))
        return float(min(1.0, max(0.0, anomaly)))

    # ------------------------------------------------------------------
    # Deterministic heuristic fallback
    # ------------------------------------------------------------------

    def _heuristic_components(
        self,
        indicators: Sequence[Indicator],
        features: np.ndarray,
    ) -> tuple[float, float]:
        """Deterministic stand-in for the XGBoost + Isolation Forest models.

        Returns ``(xgb_prob, if_score)`` both in ``[0, 1]``. The mapping is
        monotonic in the Indicator set: adding any Indicator can only keep or
        increase the severity sum and the counts, so the resulting score never
        decreases (supports P9).

        XGBoost stand-in
        ----------------
        A saturating function of the total ordinal severity. Each unit of
        ordinal severity contributes, with diminishing returns, so that a
        handful of CRITICAL Indicators approaches (but never reaches) 1.0.

        Isolation Forest stand-in
        --------------------------
        Grows with the number of *distinct* engines that fired and with the
        presence of CRITICAL Indicators — a proxy for "unusual breadth" of
        anomalous signal.
        """
        severity_sum = 0
        distinct_engines: set = set()
        critical_count = 0
        for ind in indicators:
            severity_sum += SEVERITY_ORDINAL.get(ind.severity, 0)
            distinct_engines.add(ind.engine)
            if ind.severity == Severity.CRITICAL:
                critical_count += 1

        # XGBoost stand-in: saturating curve on severity sum.
        # k chosen so a single CRITICAL (ordinal 4) => ~0.33, and large sums
        # asymptotically approach 1.0.
        xgb_prob = 1.0 - np.exp(-severity_sum / 10.0)

        # Isolation Forest stand-in: breadth of engines + critical presence.
        breadth = len(distinct_engines) / 6.0  # fraction of engines firing
        critical_signal = 1.0 - np.exp(-critical_count / 2.0)
        if_score = 0.5 * breadth + 0.5 * critical_signal

        return (
            float(min(1.0, max(0.0, xgb_prob))),
            float(min(1.0, max(0.0, if_score))),
        )


__all__ = [
    "EnsembleScorer",
    "decision_for_score",
    "XGB_MODEL_FILENAME",
    "IF_MODEL_FILENAME",
    "MODEL_VERSION_TRAINED",
    "MODEL_VERSION_HEURISTIC",
]

# Re-export SHAP helpers for callers importing from the scorer module.
__all__ += ["ShapAttributor", "assert_additivity"]

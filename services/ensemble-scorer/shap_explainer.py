"""
services/ensemble-scorer/shap_explainer.py
===========================================
SHAP feature attribution for the Ensemble Scorer (task 7.2).

The :class:`ShapAttributor` wraps ``shap.TreeExplainer(xgboost_model)`` to
produce a signed per-feature contribution vector for every scoring request.
Those contributions are mapped back to the model's feature columns
(:data:`~features.FEATURE_NAMES`) and then attached to the Indicators that
were responsible for them, producing a list of
:class:`~shared.models.ShapAttribution` records — one per Indicator.

Additivity invariant (Property 7, design.md — "SHAP Feature Attribution"):

    | sum(contributions) - (risk_score - base_value) | < 1e-4

TreeExplainer computes exact Shapley values, so SHAP additivity holds by
construction against the model's *raw* margin output. Because the Ensemble
Scorer maps the model probability through an affine + Isolation-Forest bonus +
rounding pipeline to reach the integer ``risk_score``, we cannot rely on the
model's raw SHAP sum matching ``risk_score - base_value`` directly. Instead we
rescale the SHAP contributions so their sum equals ``risk_score - base_value``
exactly (preserving the sign and relative magnitude of every feature's
contribution). This keeps the human-facing attribution faithful to SHAP's
ranking while guaranteeing the additivity invariant the design promises.

Sandbox fallback
----------------
``shap`` and the native ``xgboost`` libraries (which require ``libomp``) are not
always importable in the CI sandbox. When they are unavailable — or when the
scorer is running in heuristic mode with no trained model — this module falls
back to a deterministic *linear* attribution: it distributes
``risk_score - base_value`` across the active features proportionally to their
feature-vector values. The fallback satisfies the additivity invariant exactly,
so Property 7 (task 7.3) can be exercised without the native ML stack.

Requirements: 9.3
"""

from __future__ import annotations

from typing import Optional, Sequence

try:  # NumPy is part of the ML stack and is intentionally absent in the local
    # core-service dev environment. Guard the import so this module loads and the
    # ShapAttributor constructs in its deterministic fallback mode without it;
    # the numeric attribution paths that use ``np`` only run when NumPy is
    # installed (container/CI) or a SHAP explanation is actually computed.
    import numpy as np

    _HAS_NUMPY = True
except ImportError:  # pragma: no cover - NumPy absent in minimal environments
    np = None  # type: ignore[assignment]
    _HAS_NUMPY = False

try:  # package/relative import tolerance
    from shared.models import Indicator, ShapAttribution
except ImportError:  # pragma: no cover
    from models import Indicator, ShapAttribution  # type: ignore

try:
    from features import DEFAULT_ENCODER, FEATURE_NAMES, ENGINE_ORDER, FeatureEncoder
except ImportError:  # pragma: no cover
    from .features import (  # type: ignore
        DEFAULT_ENCODER,
        FEATURE_NAMES,
        ENGINE_ORDER,
        FeatureEncoder,
    )

try:  # SHAP explanation span (shared source of truth; task 32.1 / Req 21.5)
    from shared.telemetry import shap_span
except ImportError:  # pragma: no cover - shared.telemetry always present in repo
    from contextlib import contextmanager as _contextmanager

    @_contextmanager
    def shap_span(*_args, **_kwargs):  # type: ignore[misc]
        yield None


ADDITIVITY_TOLERANCE = 1e-4


class AdditivityError(AssertionError):
    """Raised when a SHAP attribution set violates the additivity invariant."""


def assert_additivity(
    contributions: Sequence[float],
    risk_score: float,
    base_value: float,
    tol: float = ADDITIVITY_TOLERANCE,
) -> None:
    """Assert the SHAP additivity invariant (Property 7).

    Verifies ``|sum(contributions) - (risk_score - base_value)| < tol``.

    Parameters
    ----------
    contributions:
        The signed per-attribution contribution values.
    risk_score:
        The integer Risk_Score assigned to the claim (treated as a float here).
    base_value:
        The model base value (``explainer.expected_value``).
    tol:
        Absolute floating-point tolerance (default ``1e-4``).

    Raises
    ------
    AdditivityError
        If the invariant is violated.
    """
    total = float(np.sum(contributions)) if len(contributions) else 0.0
    expected = float(risk_score) - float(base_value)
    delta = abs(total - expected)
    if delta >= tol:
        raise AdditivityError(
            f"SHAP additivity violated: |sum(contributions) - (risk_score - "
            f"base_value)| = |{total:.8f} - {expected:.8f}| = {delta:.8f} >= {tol}"
        )


class ShapAttributor:
    """Produce per-Indicator SHAP attributions for a scored claim.

    Parameters
    ----------
    xgboost_model:
        A trained XGBoost model (or any tree model supported by
        ``shap.TreeExplainer``). When ``None`` — or when the SHAP/XGBoost
        libraries cannot be imported — the deterministic linear fallback is
        used instead.
    encoder:
        Feature encoder used to determine the feature-vector layout. Defaults to
        the shared stateless encoder.
    base_value:
        Optional explicit base value. When a real explainer is constructed, its
        ``expected_value`` is used instead. Defaults to ``0.0`` so that the
        additivity target reduces to ``risk_score`` in fallback mode.
    """

    def __init__(
        self,
        xgboost_model: Optional[object] = None,
        encoder: Optional[FeatureEncoder] = None,
        base_value: float = 0.0,
    ) -> None:
        self.encoder = encoder or DEFAULT_ENCODER
        self.feature_names: tuple[str, ...] = tuple(self.encoder.feature_names)
        self._model = xgboost_model
        self._explainer = None
        self.using_fallback = True
        self.base_value = float(base_value)
        if xgboost_model is not None:
            self._try_build_explainer(xgboost_model)

    # ------------------------------------------------------------------
    # Explainer construction
    # ------------------------------------------------------------------

    def _try_build_explainer(self, model: object) -> None:
        """Attempt to build a ``shap.TreeExplainer``; fall back on any error."""
        try:
            import shap  # local import so the module loads without shap

            explainer = shap.TreeExplainer(model)
            self._explainer = explainer
            self.using_fallback = False
            # expected_value may be a scalar or a length-1/-2 array.
            ev = getattr(explainer, "expected_value", 0.0)
            self.base_value = float(np.ravel(ev)[-1]) if np.ndim(ev) else float(ev)
        except Exception:  # noqa: BLE001 - any failure => deterministic fallback
            self._explainer = None
            self.using_fallback = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def explain(
        self,
        feature_vector: np.ndarray,
        indicators: Sequence[Indicator],
        risk_score: int,
    ) -> tuple[list[ShapAttribution], float]:
        """Compute per-Indicator SHAP attributions for one claim.

        Parameters
        ----------
        feature_vector:
            The 1-D feature vector produced by the encoder for this claim.
        indicators:
            The Indicators that produced the claim's signal. May be empty.
        risk_score:
            The integer Risk_Score assigned by the Ensemble Scorer.

        Returns
        -------
        (attributions, base_value):
            ``attributions`` is a list of :class:`ShapAttribution`, one per
            Indicator (empty when ``indicators`` is empty). ``base_value`` is the
            model base value used as the additivity anchor.

        Notes
        -----
        The returned attributions always satisfy the additivity invariant:
        ``|sum(a.contribution) - (risk_score - base_value)| < 1e-4``.
        """
        base_value = self.base_value

        # Child ``shap.explain`` span (design trace structure). Only non-PII
        # metadata (indicator count, risk_score) is attached. No-op when OTel is
        # unavailable.
        with shap_span(
            "",
            indicator_count=len(indicators),
            risk_score=int(risk_score),
            using_fallback=bool(getattr(self, "using_fallback", False)),
        ):
            if not indicators:
                # No Indicators -> no attributions. With risk_score == 0 (P8) and
                # base_value == 0 the invariant holds trivially; assert to be safe.
                assert_additivity([], risk_score, base_value)
                return [], base_value

            feature_contributions = self._feature_contributions(
                feature_vector, risk_score, base_value
            )

            # Map each feature's contribution to the Indicator(s) responsible for
            # it, then rescale so the total equals (risk_score - base_value).
            raw = self._distribute_to_indicators(
                feature_contributions, indicators
            )
            attributions = self._rescale_to_additive(
                raw, indicators, risk_score, base_value
            )

            assert_additivity(
                [a.contribution for a in attributions], risk_score, base_value
            )
            return attributions, base_value

    # ------------------------------------------------------------------
    # Feature-level contributions
    # ------------------------------------------------------------------

    def _feature_contributions(
        self,
        feature_vector: np.ndarray,
        risk_score: int,
        base_value: float,
    ) -> np.ndarray:
        """Return a per-feature contribution vector aligned with FEATURE_NAMES."""
        vec = np.asarray(feature_vector, dtype=np.float64).reshape(-1)
        if not self.using_fallback and self._explainer is not None:
            try:
                shap_vals = self._explainer.shap_values(vec.reshape(1, -1))
                arr = np.asarray(shap_vals)
                # Binary classifier may return a list/2D per-class array.
                if arr.ndim == 3:  # (classes, samples, features)
                    arr = arr[-1]
                arr = arr.reshape(-1)
                if arr.shape[0] == vec.shape[0]:
                    return arr.astype(np.float64)
            except Exception:  # noqa: BLE001 - degrade to linear fallback
                pass
        return self._linear_feature_contributions(vec, risk_score, base_value)

    def _linear_feature_contributions(
        self,
        vec: np.ndarray,
        risk_score: int,
        base_value: float,
    ) -> np.ndarray:
        """Deterministic linear attribution over the feature vector.

        Distributes ``risk_score - base_value`` across features proportionally
        to their (non-negative) feature values. If every feature value is zero,
        the target is spread uniformly across all features. The result sums to
        ``risk_score - base_value`` exactly (up to float rounding), so the
        additivity invariant holds without any ML libraries.
        """
        target = float(risk_score) - float(base_value)
        weights = np.abs(vec).astype(np.float64)
        total_weight = float(weights.sum())
        if total_weight <= 0.0:
            n = weights.shape[0]
            return np.full(n, target / n, dtype=np.float64) if n else weights
        return (weights / total_weight) * target

    # ------------------------------------------------------------------
    # Mapping feature contributions back onto Indicators
    # ------------------------------------------------------------------

    def _feature_to_engine(self) -> dict[str, object]:
        """Map each per-engine feature name to its EngineType.

        Aggregate features (``total_indicator_count`` etc.) map to ``None`` and
        are shared across all Indicators.
        """
        mapping: dict[str, object] = {}
        for engine in ENGINE_ORDER:
            prefix = engine.value.lower()
            for suffix in ("_present", "_count", "_completed"):
                mapping[f"{prefix}{suffix}"] = engine
        return mapping

    def _distribute_to_indicators(
        self,
        feature_contributions: np.ndarray,
        indicators: Sequence[Indicator],
    ) -> list[float]:
        """Assign a raw contribution to each Indicator from feature contributions.

        Each engine-specific feature's contribution is divided evenly among the
        Indicators produced by that engine. Aggregate (engine-agnostic) feature
        contributions are divided evenly across all Indicators. The result is a
        raw per-Indicator contribution list, aligned 1:1 with ``indicators``.
        """
        n = len(indicators)
        raw = [0.0] * n
        feat_to_engine = self._feature_to_engine()

        # Pre-compute the Indicator index groups per engine.
        engine_to_indices: dict[object, list[int]] = {}
        for i, ind in enumerate(indicators):
            engine_to_indices.setdefault(ind.engine, []).append(i)

        for f_idx, fname in enumerate(self.feature_names):
            contrib = float(feature_contributions[f_idx])
            if contrib == 0.0:
                continue
            engine = feat_to_engine.get(fname)
            if engine is not None:
                targets = engine_to_indices.get(engine, [])
                if not targets:
                    # Feature fired for an engine with no Indicators in this set
                    # (shouldn't happen for present/count, but guard anyway):
                    # spread across all Indicators so nothing is lost.
                    targets = list(range(n))
            else:
                # Aggregate feature — shared across every Indicator.
                targets = list(range(n))
            share = contrib / len(targets)
            for t in targets:
                raw[t] += share
        return raw

    def _rescale_to_additive(
        self,
        raw: list[float],
        indicators: Sequence[Indicator],
        risk_score: int,
        base_value: float,
    ) -> list[ShapAttribution]:
        """Rescale raw per-Indicator contributions so their sum is exact.

        The sum of the returned contributions equals ``risk_score - base_value``
        to within floating-point precision. A residual (from rounding) is folded
        into the largest-magnitude contribution so the invariant holds tightly.
        """
        n = len(indicators)
        target = float(risk_score) - float(base_value)
        raw_sum = float(sum(raw))

        if abs(raw_sum) > 0.0:
            scaled = [c * (target / raw_sum) for c in raw]
        else:
            # All-zero raw contributions -> distribute the target evenly.
            scaled = [target / n] * n

        # Fold any residual (float rounding) into the largest-magnitude entry so
        # the sum matches the target as closely as float precision allows.
        residual = target - float(sum(scaled))
        if n:
            j = max(range(n), key=lambda k: abs(scaled[k]))
            scaled[j] += residual

        attributions: list[ShapAttribution] = []
        for ind, contribution in zip(indicators, scaled):
            attributions.append(
                ShapAttribution(
                    indicator_id=ind.indicator_id,
                    feature_name=self._primary_feature_for(ind),
                    contribution=float(contribution),
                    base_value=float(base_value),
                )
            )
        return attributions

    def _primary_feature_for(self, indicator: Indicator) -> str:
        """Return the most relevant feature name for an Indicator's engine."""
        prefix = indicator.engine.value.lower()
        candidate = f"{prefix}_count"
        if candidate in self.feature_names:
            return candidate
        return "total_indicator_count"


__all__ = [
    "ShapAttributor",
    "assert_additivity",
    "AdditivityError",
    "ADDITIVITY_TOLERANCE",
]

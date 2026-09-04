"""
services/ensemble-scorer/train_stub.py
=======================================
Development/local training stub for the Ensemble Scorer.

This script trains **placeholder** XGBoost and Isolation Forest models on
synthetic data and saves them to ``models/xgboost_v1.joblib`` and
``models/isolation_forest_v1.joblib`` so the service has loadable artefacts for
local development, integration tests, and container smoke tests.

These models are NOT production models. They are trained on synthetic feature
vectors generated from randomly constructed Indicator sets, with a simple,
severity-driven label so the classifier learns the intended direction
(more/severer Indicators => higher fraud probability).

Usage
-----
    python services/ensemble-scorer/train_stub.py [--out models] [--n 4000] [--seed 42]

Requires: numpy, scikit-learn, xgboost, joblib.
"""

from __future__ import annotations

import argparse
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

try:
    from shared.models import EngineType, Indicator, Severity
except ImportError:  # pragma: no cover
    from models import EngineType, Indicator, Severity  # type: ignore

try:
    from features import DEFAULT_ENCODER, ENGINE_ORDER, SEVERITY_ORDINAL
except ImportError:  # pragma: no cover
    from .features import DEFAULT_ENCODER, ENGINE_ORDER, SEVERITY_ORDINAL  # type: ignore


_SEVERITIES = [
    Severity.LOW,
    Severity.MEDIUM,
    Severity.HIGH,
    Severity.CRITICAL,
]


def _make_indicator(engine: EngineType, severity: Severity) -> Indicator:
    """Build a minimal, valid Indicator for feature encoding."""
    return Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        engine=engine,
        severity=severity,
        algorithm="synthetic",
        title="synthetic indicator",
        explanation_en="synthetic",
        explanation_fr="synthetique",
        payload={},
        produced_at=datetime.now(timezone.utc),
    )


def _random_indicator_set(rng: random.Random) -> list[Indicator]:
    """Generate a random Indicator set of 0..12 Indicators."""
    n = rng.randint(0, 12)
    out: list[Indicator] = []
    for _ in range(n):
        engine = rng.choice(list(ENGINE_ORDER))
        severity = rng.choice(_SEVERITIES)
        out.append(_make_indicator(engine, severity))
    return out


def _label_for(indicators: list[Indicator]) -> int:
    """Synthetic fraud label: driven by total ordinal severity.

    A claim is labelled fraudulent (1) when its accumulated ordinal severity
    crosses a threshold, with a little noise so the classifier does not become
    a perfect step function. This gives the placeholder model the correct
    monotone direction.
    """
    severity_sum = sum(SEVERITY_ORDINAL.get(i.severity, 0) for i in indicators)
    # Threshold ~ two HIGH indicators.
    return 1 if severity_sum >= 6 else 0


def generate_dataset(n: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Generate ``n`` synthetic (features, label) rows."""
    rng = random.Random(seed)
    encoder = DEFAULT_ENCODER
    X = np.zeros((n, encoder.vector_length), dtype=np.float64)
    y = np.zeros(n, dtype=np.int64)
    for i in range(n):
        indicators = _random_indicator_set(rng)
        X[i] = encoder.encode(indicators)
        label = _label_for(indicators)
        # Inject light label noise (~5%).
        if rng.random() < 0.05:
            label = 1 - label
        y[i] = label
    return X, y


def train_and_save(out_dir: Path, n: int, seed: int) -> tuple[Path, Path]:
    """Train placeholder models and persist them to ``out_dir``."""
    import joblib
    from sklearn.ensemble import IsolationForest
    from xgboost import XGBClassifier

    out_dir.mkdir(parents=True, exist_ok=True)

    X, y = generate_dataset(n, seed)

    # Guard against a degenerate single-class dataset.
    if len(np.unique(y)) < 2:
        y[0] = 0
        y[-1] = 1

    xgb = XGBClassifier(
        n_estimators=100,
        max_depth=4,
        learning_rate=0.1,
        subsample=0.9,
        random_state=seed,
        eval_metric="logloss",
    )
    xgb.fit(X, y)

    iforest = IsolationForest(
        n_estimators=100,
        contamination=0.1,
        random_state=seed,
    )
    iforest.fit(X)

    xgb_path = out_dir / "xgboost_v1.joblib"
    if_path = out_dir / "isolation_forest_v1.joblib"
    joblib.dump(xgb, xgb_path)
    joblib.dump(iforest, if_path)
    return xgb_path, if_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Train placeholder ensemble models.")
    default_out = Path(__file__).resolve().parent / "models"
    parser.add_argument("--out", type=Path, default=default_out, help="Output model directory.")
    parser.add_argument("--n", type=int, default=4000, help="Number of synthetic rows.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed.")
    args = parser.parse_args()

    xgb_path, if_path = train_and_save(args.out, args.n, args.seed)
    print(f"Saved XGBoost model to:          {xgb_path}")
    print(f"Saved Isolation Forest model to: {if_path}")


if __name__ == "__main__":
    main()

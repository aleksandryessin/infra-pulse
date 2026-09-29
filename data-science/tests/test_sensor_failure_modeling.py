"""Fit sample, weights, feature sets and baseline for sensor-failure-model-v1."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from infra_pulse_research.modeling.sensor_failure_modeling import (
    FittedSet,
    feature_columns,
    fit_weights,
    negative_sample_mask,
    persistence_score,
    signed_log1p,
)

CONFIG = json.loads(
    (Path(__file__).resolve().parents[1] / "configs/sensor_failure_model_v1.json").read_text(
        encoding="utf-8"
    )
)


def test_feature_sets_are_nested_and_numeric_only_in_f3() -> None:
    columns = ["channel_id", "hist__a", "cal__dow", "cat__sensor_type", "ctx__b", "num__z"]
    f1, f2, f3 = (set(feature_columns(columns, CONFIG, s)) for s in ("F1", "F2", "F3"))
    assert f1 < f2 < f3
    assert f2 - f1 == {"ctx__b"} and f3 - f2 == {"num__z"}
    assert "channel_id" not in f3


def test_negative_sample_is_deterministic_and_label_free() -> None:
    ids = pd.Series(np.arange(2000))
    days = pd.Series(pd.date_range("2024-01-01", periods=2000, freq="D"))
    first = negative_sample_mask(ids, days)
    second = negative_sample_mask(
        ids.iloc[::-1].reset_index(drop=True), days[::-1].reset_index(drop=True)
    )
    assert first.sum() == second.sum()
    assert 140 < first.sum() < 260


def test_weights_undo_sampling_then_balance() -> None:
    y = np.array([1, 0, 0, 0])
    w = fit_weights(y)
    assert np.isclose(w[y == 1].sum(), w[y == 0].sum())
    with pytest.raises(ValueError):
        fit_weights(np.zeros(3, dtype=int))


def test_persistence_prefers_recent_candidates() -> None:
    frame = pd.DataFrame(
        {
            "hist__connection_loss__hours_since_last_candidate": [2.0, 50.0, None, 2.0],
            "hist__connection_loss__starts_30d": [1, 3, 0, 4],
        }
    )
    score = persistence_score(frame, "connection_loss")
    assert list(np.argsort(-score, kind="stable")) == [3, 0, 1, 2]
    assert score[2] == 0.0


def test_signed_log_keeps_sign_and_zero() -> None:
    assert np.allclose(signed_log1p(np.array([-127.0, 0.0, 3.0])), [-np.log1p(127), 0, np.log1p(3)])


def test_fitted_set_scores_every_row() -> None:
    rng = np.random.default_rng(5)
    n = 600
    frame = pd.DataFrame(
        {
            "hist__connection_loss__hours_since_last_candidate": rng.exponential(100, n),
            "hist__x": rng.normal(size=n),
            "ctx__y": rng.normal(size=n),
            "num__z": rng.normal(size=n),
            "cal__dow": rng.integers(0, 7, n).astype(float),
            "cat__sensor_type": rng.choice(["Датчик дыма", "КД Дверь"], n),
            "num__base_reason": rng.choice(["ok", "insufficient_support", None], n),
        }
    )
    y = (frame.hist__x + rng.normal(scale=0.5, size=n) > 1).astype(int).to_numpy()
    fitted = FittedSet(frame, y, CONFIG, "F3")
    scores = fitted.score(frame)
    assert set(scores) == {"logreg", "catboost"}
    assert all(len(v) == n and np.all((v >= 0) & (v <= 1)) for v in scores.values())
    assert fitted.meta["logreg_converged"]
    assert "num__base_reason" in fitted.categorical

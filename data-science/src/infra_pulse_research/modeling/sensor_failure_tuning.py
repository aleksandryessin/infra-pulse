"""Candidate scoring for ``sensor-failure-tuning-v2`` (frozen, user-confirmed).

CatBoost only (no LogisticRegression); persistence is the untrained reference.
Channel scores become object × sensor-type card scores by a declared
aggregation, optionally combined with persistence by within-day ranks. All
selection happens on development folds; the holdout is scored once at the end.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from sklearn.metrics import average_precision_score

from infra_pulse_research.modeling.sensor_failure_modeling import (
    fit_weights,
    matrix,
    persistence_score,
    split_kinds,
)

CARD_KEYS = ["object_id", "sensor_type", "issued_at"]
PROXY_PATTERN = "_other_channels"


def variant_columns(columns: list[str], config: dict, variant: str) -> list[str]:
    """Frozen feature variants F2, F3 and F3 without object-identity proxies."""
    sets = config["candidates"]["feature_sets"]
    base = "F3" if variant == "F3_noproxy" else variant
    include = tuple(sets[base])
    selected = sorted(c for c in columns if c.startswith(include))
    if variant == "F3_noproxy":
        selected = [c for c in selected if not (c.startswith("ctx__") and PROXY_PATTERN in c)]
    if variant == "F2" and any(c.startswith("num__") for c in selected):
        raise ValueError("F2 must not contain value_numeric features")
    return selected


class FittedCatBoost:
    """One CatBoost configuration fitted on a development fold sample."""

    def __init__(
        self, sample: pd.DataFrame, y: np.ndarray, columns: list[str], params: dict, fixed: dict
    ):
        self.numeric, self.categorical = split_kinds(columns, sample)
        x = matrix(sample, self.numeric, self.categorical)
        self.model = CatBoostClassifier(**fixed, **params, verbose=False, allow_writing_files=False)
        self.model.fit(x, y, cat_features=self.categorical, sample_weight=fit_weights(y))
        importance = self.model.get_feature_importance(type="PredictionValuesChange")
        order = np.argsort(-importance)[:10]
        self.top_features = [(x.columns[i], round(float(importance[i]), 3)) for i in order]

    def score(self, frame: pd.DataFrame) -> np.ndarray:
        return self.model.predict_proba(matrix(frame, self.numeric, self.categorical))[:, 1]


def card_aggregates(channels: pd.DataFrame, score_col: str, how: str) -> pd.DataFrame:
    """Card score from candidate channel scores: max, noisy-OR or mean of the top two."""
    rows = channels.loc[channels["candidate"].astype(bool), [*CARD_KEYS, score_col]].copy()
    if how == "max":
        out = rows.groupby(CARD_KEYS, sort=False)[score_col].max()
    elif how == "noisy_or":
        rows["log_keep"] = np.log1p(-np.clip(rows[score_col].to_numpy(dtype=float), 0, 1 - 1e-12))
        out = 1.0 - np.exp(rows.groupby(CARD_KEYS, sort=False)["log_keep"].sum())
    elif how == "mean_top2":
        rows = rows.sort_values([*CARD_KEYS, score_col], ascending=[True, True, True, False])
        top = rows[rows.groupby(CARD_KEYS, sort=False).cumcount() < 2]
        out = top.groupby(CARD_KEYS, sort=False)[score_col].mean()
    else:
        raise ValueError(f"unknown aggregation: {how}")
    return out.rename("score").reset_index()


def day_rank(frame: pd.DataFrame, column: str) -> pd.Series:
    """Within-cutoff percentile rank (1 = highest); ties share the average rank."""
    return frame.groupby("issued_at")[column].rank(pct=True, method="average")


def combine(
    cards: pd.DataFrame,
    model_col: str,
    persistence_col: str,
    how: str,
    routing: dict[str, str] | None = None,
) -> np.ndarray:
    """Declared combinations of the model card score with persistence."""
    if how == "model_only":
        return cards[model_col].to_numpy(dtype=float)
    model_rank = day_rank(cards, model_col).to_numpy(dtype=float)
    persistence_rank = day_rank(cards, persistence_col).to_numpy(dtype=float)
    if how == "rank_mean_with_persistence":
        return 0.5 * (model_rank + persistence_rank)
    if how == "per_sensor_type_best_of_model_or_persistence_by_folds":
        if routing is None:
            raise ValueError("routing is required")
        use_model = cards["sensor_type"].map(routing).fillna("persistence").eq("model").to_numpy()
        return np.where(use_model, model_rank, persistence_rank)
    raise ValueError(f"unknown combination: {how}")


def routing_by_type(cards: pd.DataFrame, model_col: str, persistence_col: str) -> dict[str, str]:
    """Per sensor type, pick the scorer with the higher card PR-AUC on these cards.

    Used cross-fitted: routing learned on one development fold is applied to
    the other, so no card scores its own routing.
    """
    routing = {}
    if "outcome" in cards:
        cards = cards[cards["outcome"].isin(["positive", "negative"])]
    for sensor_type, part in cards.groupby("sensor_type"):
        y = part["y_card"].to_numpy(dtype=int)
        if y.min() == y.max():
            routing[str(sensor_type)] = "persistence"
            continue
        model = average_precision_score(y, part[model_col].fillna(0))
        persistence = average_precision_score(y, part[persistence_col].fillna(0))
        routing[str(sensor_type)] = "model" if model > persistence else "persistence"
    return routing


def channel_persistence(frame: pd.DataFrame, label: str) -> np.ndarray:
    return persistence_score(frame, label)

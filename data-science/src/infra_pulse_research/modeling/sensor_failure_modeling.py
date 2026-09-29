"""Persistence, LogisticRegression and CatBoost for ``sensor-failure-model-v1``.

Channel scores are rankings; the evaluation module aggregates them into
object × sensor-type cards and calibrates card scores on the calibration
period only. Preprocessing and the fit sample come from development only.
"""

from __future__ import annotations

import hashlib
import warnings

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from sklearn.compose import ColumnTransformer
from sklearn.exceptions import ConvergenceWarning
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, RobustScaler

from infra_pulse_research.modeling.subsystem_modeling import (
    categorical_frame,
    drop_uninformative,
    numeric_frame,
)

KEYS = ("channel_id", "issued_at")
CATEGORICAL_PREFIXES = ("cat__",)
SAMPLE_SALT = "sensor-failure-model-v1"


def feature_columns(columns: list[str], config: dict, feature_set: str) -> list[str]:
    include = tuple(config["feature_sets"][feature_set]["include"])
    selected = sorted(c for c in columns if c.startswith(include))
    if feature_set != "F3" and any(c.startswith("num__") for c in selected):
        raise ValueError(f"{feature_set} must not contain value_numeric features")
    return selected


def split_kinds(
    columns: list[str], frame: pd.DataFrame | None = None
) -> tuple[list[str], list[str]]:
    """``cat__`` and any non-numeric column (e.g. a reason code) are categorical."""
    categorical = [
        c
        for c in columns
        if c.startswith(CATEGORICAL_PREFIXES)
        or (frame is not None and not pd.api.types.is_numeric_dtype(frame[c]))
    ]
    return [c for c in columns if c not in categorical], categorical


def negative_sample_mask(channel_id: pd.Series, issued_at: pd.Series, *, percent: int = 10):
    """Deterministic hash sample of rows; independent of labels and scores."""
    stamps = pd.to_datetime(issued_at).dt.strftime("%Y-%m-%dT%H:%M:%S")
    keys = SAMPLE_SALT + ":" + channel_id.astype("int64").astype(str) + ":" + stamps
    buckets = keys.map(lambda k: int(hashlib.sha256(k.encode()).hexdigest(), 16) % 100)
    return (buckets < percent).to_numpy()


def fit_weights(y: np.ndarray, *, negative_weight: float = 10.0) -> np.ndarray:
    """Undo the negative sample, then balance classes like ``class_weight='balanced'``."""
    base = np.where(y == 1, 1.0, negative_weight)
    positive, negative = base[y == 1].sum(), base[y == 0].sum()
    if positive == 0 or negative == 0:
        raise ValueError("fit sample needs both classes")
    total = positive + negative
    return base * np.where(y == 1, total / (2 * positive), total / (2 * negative))


def signed_log1p(values):
    return np.sign(values) * np.log1p(np.abs(values))


def logreg_pipeline(numeric: list[str], categorical: list[str], params: dict) -> Pipeline:
    transformers = [
        (
            "numeric",
            Pipeline(
                [
                    (
                        "impute",
                        SimpleImputer(
                            strategy="median", add_indicator=True, keep_empty_features=True
                        ),
                    ),
                    ("compress", FunctionTransformer(signed_log1p, feature_names_out="one-to-one")),
                    ("scale", RobustScaler()),
                ]
            ),
            numeric,
        )
    ]
    if categorical:
        transformers.append(
            (
                "categorical",
                OneHotEncoder(handle_unknown="infrequent_if_exist", min_frequency=20),
                categorical,
            )
        )
    return Pipeline(
        [
            ("prepare", ColumnTransformer(transformers, sparse_threshold=0.0)),
            (
                "model",
                LogisticRegression(
                    C=params.get("C", 1.0),
                    max_iter=params.get("max_iter", 5000),
                    solver=params.get("solver", "lbfgs"),
                ),
            ),
        ]
    )


def matrix(frame: pd.DataFrame, numeric: list[str], categorical: list[str]) -> pd.DataFrame:
    return pd.concat([numeric_frame(frame, numeric), categorical_frame(frame, categorical)], axis=1)


def persistence_score(frame: pd.DataFrame, label: str) -> np.ndarray:
    """Recency of the last candidate; ties by episode starts in 30 days; never = lowest."""
    hours = frame[f"hist__{label}__hours_since_last_candidate"].astype(float)
    starts = frame[f"hist__{label}__starts_30d"].fillna(0).astype(float)
    recency = 1.0 / (1.0 + hours.fillna(np.inf).to_numpy())
    return recency + 1e-6 * np.minimum(starts.to_numpy(), 1e5) / 1e5


class FittedSet:
    """LR and CatBoost fitted on one development sample for one feature set."""

    def __init__(self, sample: pd.DataFrame, y: np.ndarray, config: dict, feature_set: str):
        columns = feature_columns(list(sample.columns), config, feature_set)
        kept, dropped = drop_uninformative(sample, columns)
        self.numeric, self.categorical = split_kinds(kept, sample)
        weights = fit_weights(y)
        x = matrix(sample, self.numeric, self.categorical)
        self.logreg = logreg_pipeline(self.numeric, self.categorical, config["models"]["logreg"])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ConvergenceWarning)
            self.logreg.fit(x, y, model__sample_weight=weights)
        self.catboost = CatBoostClassifier(
            **config["models"]["catboost"], verbose=False, allow_writing_files=False
        )
        self.catboost.fit(x, y, cat_features=self.categorical, sample_weight=weights)
        importance = self.catboost.get_feature_importance(type="PredictionValuesChange")
        by_prefix: dict[str, float] = {}
        for name, value in zip(x.columns, importance, strict=True):
            prefix = name.split("__", 1)[0] + "__"
            by_prefix[prefix] = by_prefix.get(prefix, 0.0) + float(value)
        total = sum(by_prefix.values()) or 1.0
        top = np.argsort(-importance)[:15]
        self.meta = {
            "feature_set": feature_set,
            "candidate_columns": len(columns),
            "kept_numeric": len(self.numeric),
            "kept_categorical": self.categorical,
            "dropped_constant": len(dropped["constant"]),
            "dropped_duplicate": len(dropped["duplicate_of"]),
            "fit_rows": len(sample),
            "fit_positive": int(y.sum()),
            "logreg_iterations": int(np.max(self.logreg.named_steps["model"].n_iter_)),
            "logreg_converged": not any(issubclass(w.category, ConvergenceWarning) for w in caught),
            "catboost_importance_by_prefix": {
                k: round(v / total, 4) for k, v in sorted(by_prefix.items(), key=lambda kv: -kv[1])
            },
            "catboost_top_features": [
                {"feature": x.columns[i], "importance": round(float(importance[i]), 3)} for i in top
            ],
        }

    def score(self, frame: pd.DataFrame) -> dict[str, np.ndarray]:
        x = matrix(frame, self.numeric, self.categorical)
        return {
            "logreg": self.logreg.predict_proba(x)[:, 1],
            "catboost": self.catboost.predict_proba(x)[:, 1],
        }

"""Rule, LogisticRegression and CatBoost for ``subsystem-alarm-v1`` sets A–E.

Every model reads the same group rows and labels. Preprocessing, constant and
duplicate filtering are fitted on development only; the threshold is chosen on
validation only by the evaluation module. Identifiers stay keys, never inputs.
"""

from __future__ import annotations

import hashlib
import json
import warnings
from pathlib import Path
from time import perf_counter

import duckdb
import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from sklearn.compose import ColumnTransformer
from sklearn.exceptions import ConvergenceWarning
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, RobustScaler

KEYS = ("object_id", "system_type", "issued_at")
CATEGORICAL_PREFIXES = ("cat__", "name__")
NEVER_INPUT_PREFIXES = ("card__",)
FIT_PERIOD = "development"
THRESHOLD_PERIOD = "validation_2025_h1"
MISSING = "__missing__"


def _literal(path: Path | str) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def feature_columns(columns: list[str], config: dict, feature_set: str) -> list[str]:
    """Columns of a frozen set, in stable order; numeric-derived D exclusion by prefix."""
    include = tuple(config["feature_sets"][feature_set]["include"])
    selected = [
        name
        for name in columns
        if name.startswith(include) and not name.startswith(NEVER_INPUT_PREFIXES)
    ]
    if feature_set == "D" and any("_num__" in name for name in selected):
        raise ValueError("set D must not contain value_numeric features")
    return sorted(selected)


def split_kinds(columns: list[str]) -> tuple[list[str], list[str]]:
    categorical = [c for c in columns if c.startswith(CATEGORICAL_PREFIXES)]
    numeric = [c for c in columns if c not in categorical]
    return numeric, categorical


def drop_uninformative(development: pd.DataFrame, columns: list[str]) -> tuple[list[str], dict]:
    """Remove development-constant and exact duplicate columns (first name kept)."""
    kept, constant, duplicate = [], [], {}
    seen: dict[str, str] = {}
    for name in columns:
        values = development[name]
        if values.nunique(dropna=False) <= 1:
            constant.append(name)
            continue
        digest = hashlib.sha256(
            pd.util.hash_pandas_object(values.astype("string"), index=False).to_numpy().tobytes()
        ).hexdigest()
        if digest in seen:
            duplicate[name] = seen[digest]
            continue
        seen[digest] = name
        kept.append(name)
    return kept, {"constant": constant, "duplicate_of": duplicate}


def categorical_frame(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = frame.loc[:, columns].astype("string").fillna(MISSING)
    return out.astype(str)


def numeric_frame(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = frame.loc[:, columns].apply(pd.to_numeric, errors="raise")
    return out.astype("float64").replace([np.inf, -np.inf], np.nan)


def model_matrix(frame: pd.DataFrame, numeric: list[str], categorical: list[str]) -> pd.DataFrame:
    return pd.concat([numeric_frame(frame, numeric), categorical_frame(frame, categorical)], axis=1)


def logreg_pipeline(numeric: list[str], categorical: list[str], params: dict) -> Pipeline:
    transformers = [
        (
            "numeric",
            Pipeline(
                [
                    (
                        "impute",
                        SimpleImputer(
                            strategy="median",
                            add_indicator=params.get("missing_indicators", True),
                            keep_empty_features=True,
                        ),
                    ),
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
                OneHotEncoder(
                    handle_unknown="infrequent_if_exist",
                    min_frequency=20,
                    sparse_output=True,
                ),
                categorical,
            )
        )
    return Pipeline(
        [
            ("prepare", ColumnTransformer(transformers, sparse_threshold=0.0)),
            (
                "model",
                LogisticRegression(
                    class_weight="balanced",
                    C=params.get("C", 1.0),
                    max_iter=params.get("max_iter", 2000),
                    solver=params.get("solver", "lbfgs"),
                ),
            ),
        ]
    )


def catboost_model(params: dict) -> CatBoostClassifier:
    return CatBoostClassifier(**params, verbose=False, allow_writing_files=False)


def check_fit_rows(frame: pd.DataFrame) -> pd.Series:
    """Development rows with a known binary label; excluded rows never enter a fit."""
    mask = (frame["split_period"] == FIT_PERIOD) & frame["outcome"].isin(["positive", "negative"])
    y = frame.loc[mask, "y"]
    if y.isna().any() or not set(y.unique()) <= {0, 1}:
        raise ValueError("fit rows contain unknown or non-binary labels")
    if y.nunique() != 2:
        raise ValueError("development needs both classes")
    return mask


def rule_share_score(frame: pd.DataFrame) -> np.ndarray:
    """Lexicographic rank: alarming-channel share desc, then last-alarm age asc."""
    share = frame["own_sig__alarm_channel_share_24h"].fillna(0.0).to_numpy(float)
    age = frame["own_sig__hours_since_last_alarm"].fillna(np.inf).to_numpy(float)
    order = np.lexsort((age, -share))
    ranks = np.empty(len(frame), dtype=float)
    # Equal (share, age) pairs keep an equal score, so tie-breaking stays in card_mask.
    keys = np.stack([share[order], age[order]], axis=1)
    new_value = np.r_[True, np.any(keys[1:] != keys[:-1], axis=1)]
    dense = np.cumsum(new_value)
    ranks[order] = dense
    return 1.0 - (ranks - 1) / max(dense[-1], 1)


def rule_repeat_score(frame: pd.DataFrame) -> np.ndarray:
    return (frame["own_sig__alarm_rows_24h"].fillna(0).to_numpy(float) > 0).astype(float)


def load_matrix(
    features_dir: Path, labels_dir: Path, *, memory_limit: str = "12GB"
) -> pd.DataFrame:
    """Join group features and labels; exactly one row per group cutoff."""
    con = duckdb.connect()
    con.execute(f"SET memory_limit={_literal(memory_limit)}")
    features = Path(features_dir) / "group/month=*/group_features.parquet"
    labels = Path(labels_dir) / "month=*/labels.parquet"
    frame = con.execute(f"""
        SELECT f.*, l.* EXCLUDE (object_id, system_type, issued_at, month)
        FROM read_parquet({_literal(features)}, union_by_name=true) f
        JOIN read_parquet({_literal(labels)}, union_by_name=true) l
          USING (object_id, system_type, issued_at)
    """).df()
    counts = con.execute(f"""
        SELECT (SELECT COUNT(*) FROM read_parquet({_literal(features)}, union_by_name=true)),
               (SELECT COUNT(*) FROM read_parquet({_literal(labels)}, union_by_name=true))
    """).fetchone()
    con.close()
    if frame.duplicated(list(KEYS)).any():
        raise ValueError("duplicate group cutoffs")
    if not len(frame) == counts[0] == counts[1]:
        raise ValueError(f"features/labels row mismatch: joined={len(frame)}, sources={counts}")
    frame["issued_at"] = pd.to_datetime(frame["issued_at"])
    return frame.sort_values(list(KEYS), kind="mergesort").reset_index(drop=True)


def _prefix_importance(names: list[str], values: np.ndarray) -> dict[str, float]:
    total = float(np.sum(values)) or 1.0
    out: dict[str, float] = {}
    for name, value in zip(names, values, strict=True):
        prefix = name.split("__", 1)[0] + "__"
        out[prefix] = out.get(prefix, 0.0) + float(value) / total
    return {k: round(v, 4) for k, v in sorted(out.items(), key=lambda kv: -kv[1])}


def fit_feature_set(
    frame: pd.DataFrame,
    config: dict,
    feature_set: str,
    *,
    fit_mask: pd.Series | None = None,
) -> dict:
    """Fit LR and CatBoost for one set; return scores for every row and metadata."""
    fit_mask = check_fit_rows(frame) if fit_mask is None else fit_mask
    columns = feature_columns(list(frame.columns), config, feature_set)
    kept, dropped = drop_uninformative(frame.loc[fit_mask], columns)
    numeric, categorical = split_kinds(kept)
    x_all = model_matrix(frame, numeric, categorical)
    y_fit = frame.loc[fit_mask, "y"].astype(int).to_numpy()
    meta = {
        "feature_set": feature_set,
        "candidate_columns": len(columns),
        "kept_numeric": len(numeric),
        "kept_categorical": categorical,
        "dropped_constant": len(dropped["constant"]),
        "dropped_duplicate": len(dropped["duplicate_of"]),
        "fit_rows": int(fit_mask.sum()),
        "fit_positive": int(y_fit.sum()),
    }
    scores = {}
    started = perf_counter()
    lr = logreg_pipeline(numeric, categorical, config["models"]["logreg"])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ConvergenceWarning)
        lr.fit(x_all.loc[fit_mask], y_fit)
    meta["logreg_seconds"] = round(perf_counter() - started, 2)
    meta["logreg_iterations"] = int(np.max(lr.named_steps["model"].n_iter_))
    meta["logreg_converged"] = not any(issubclass(w.category, ConvergenceWarning) for w in caught)
    scores["logreg"] = lr.predict_proba(x_all)[:, 1]
    started = perf_counter()
    cb = catboost_model(config["models"]["catboost"])
    cb.fit(x_all.loc[fit_mask], y_fit, cat_features=categorical)
    meta["catboost_seconds"] = round(perf_counter() - started, 2)
    scores["catboost"] = cb.predict_proba(x_all)[:, 1]
    importance = cb.get_feature_importance(type="PredictionValuesChange")
    names = list(x_all.columns)
    top = np.argsort(-importance)[:20]
    meta["catboost_importance_by_prefix"] = _prefix_importance(names, importance)
    meta["catboost_top_features"] = [
        {"feature": names[i], "importance": round(float(importance[i]), 3)} for i in top
    ]
    return {"scores": scores, "meta": meta, "columns": kept}


def heldout_objects(object_ids: pd.Series, config: dict) -> pd.Series:
    """Fixed hash split of objects for the name-transfer check (20% held out)."""
    salt = config["version"]

    def is_heldout(value: int) -> bool:
        digest = hashlib.sha256(f"{salt}:{int(value)}".encode()).hexdigest()
        return int(digest, 16) % 5 == 0

    lookup = {v: is_heldout(v) for v in object_ids.unique()}
    return object_ids.map(lookup).astype(bool)


def name_transfer(frame: pd.DataFrame, config: dict) -> dict:
    """Fit C and E on held-in development objects; compare on validation by object group."""
    heldout = heldout_objects(frame["object_id"], config)
    base_mask = check_fit_rows(frame)
    fit_mask = base_mask & ~heldout
    valid = (frame["split_period"] == THRESHOLD_PERIOD) & frame["outcome"].isin(
        ["positive", "negative"]
    )
    result = {
        "objects_heldout": int(frame.loc[heldout, "object_id"].nunique()),
        "objects_heldin": int(frame.loc[~heldout, "object_id"].nunique()),
        "fit_rows": int(fit_mask.sum()),
        "sets": {},
    }
    for feature_set in ("C", "E"):
        fitted = fit_feature_set(frame, config, feature_set, fit_mask=fit_mask)
        per_model = {}
        for model, score in fitted["scores"].items():
            parts = {}
            for part, mask in (("heldin", valid & ~heldout), ("heldout", valid & heldout)):
                y = frame.loc[mask, "y"].astype(int).to_numpy()
                s = score[mask.to_numpy()]
                parts[part] = {
                    "rows": int(mask.sum()),
                    "positives": int(y.sum()),
                    "objects": int(frame.loc[mask, "object_id"].nunique()),
                    "pr_auc": (
                        round(float(average_precision_score(y, s)), 4)
                        if 0 < y.sum() < len(y)
                        else None
                    ),
                    "roc_auc": (
                        round(float(roc_auc_score(y, s)), 4) if 0 < y.sum() < len(y) else None
                    ),
                    "base_rate": round(float(y.mean()), 4) if len(y) else None,
                }
            per_model[model] = parts
        result["sets"][feature_set] = {"models": per_model, "fit": fitted["meta"]}
    for model in ("logreg", "catboost"):
        deltas = {}
        for part in ("heldin", "heldout"):
            e = result["sets"]["E"]["models"][model][part]["pr_auc"]
            c = result["sets"]["C"]["models"][model][part]["pr_auc"]
            deltas[part] = None if e is None or c is None else round(e - c, 4)
        result[f"{model}_pr_auc_delta_E_minus_C"] = deltas
    return result


def score_all(frame: pd.DataFrame, config: dict, sets: tuple[str, ...] = ("A", "B", "C", "D", "E")):
    """Return a scores frame (keys + ``score__*``) and per-set fit metadata."""
    scores = frame.loc[:, list(KEYS)].copy()
    scores["score__rule_share"] = rule_share_score(frame)
    scores["score__rule_repeat"] = rule_repeat_score(frame)
    fits = {}
    for feature_set in sets:
        fitted = fit_feature_set(frame, config, feature_set)
        for model, values in fitted["scores"].items():
            scores[f"score__{model}_{feature_set}"] = values
        fits[feature_set] = fitted["meta"]
        print(
            f"set {feature_set}: {fitted['meta']['kept_numeric']} numeric + "
            f"{len(fitted['meta']['kept_categorical'])} categorical; "
            f"LR {fitted['meta']['logreg_seconds']} s, "
            f"CatBoost {fitted['meta']['catboost_seconds']} s",
            flush=True,
        )
    return scores, fits


def config_sha256(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_config(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))

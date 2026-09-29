"""Card-budget evaluation for ``subsystem-alarm-v1`` group-day scores.

Metrics use only rows with ``outcome`` in positive/negative; excluded rows are
counted by reason. A card is one ``object_id × system_type`` row on one
``issued_at`` day; the daily budget is applied among evaluable rows. Outputs are
JSON-serializable aggregates without object or channel identifiers.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score

CONFIG_PATH = Path(__file__).resolve().parents[3] / "configs" / "subsystem_alarm_v1.json"
EVALUABLE = ("positive", "negative")
STRATA = ("continuation", "onset_after_24h_quiet")
BLOCK_SCHEMES = ("iso_week", "object_id")
_BATCH = 50


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _py(value):
    """Convert numpy/pandas scalars and containers to plain JSON values."""
    if isinstance(value, dict):
        return {str(k): _py(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_py(v) for v in value]
    if isinstance(value, np.bool_ | bool):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating | float):
        value = float(value)
        return None if math.isnan(value) or math.isinf(value) else value
    return value


def _days(frame: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(frame["issued_at"]).dt.normalize()


def _labels(frame: pd.DataFrame) -> np.ndarray:
    outcome = frame["outcome"].to_numpy()
    if not np.isin(outcome, EVALUABLE).all():
        raise ValueError("expected evaluable rows only")
    y = (outcome == "positive").astype(np.int8)
    if "y" in frame and not np.array_equal(frame["y"].to_numpy(dtype=float), y):
        raise ValueError("y disagrees with outcome")
    return y


def _evaluable(frame: pd.DataFrame, score_cols: list[str]) -> pd.DataFrame:
    unknown = set(frame["outcome"].unique()) - {*EVALUABLE, "excluded"}
    if unknown:
        raise ValueError(f"unknown outcomes: {sorted(unknown)}")
    rows = frame[frame["outcome"].isin(EVALUABLE)].reset_index(drop=True)
    for col in score_cols:
        if not np.isfinite(rows[col].to_numpy(dtype=float)).all():
            raise ValueError(f"{col} has non-finite scores on evaluable rows")
    return rows


def card_mask(
    frame: pd.DataFrame,
    score_col: str,
    *,
    budget: int | None = 10,
    threshold: float | None = None,
) -> np.ndarray:
    """Cards per ``issued_at`` day: score >= threshold, top ``budget`` by score.

    ``budget=None`` means no daily cap (every eligible row is a card).

    Ties are broken by ``object_id`` then ``system_type`` (ascending). Rows must
    be unique per group-day, so a group gets at most one card per day.
    """
    if budget is not None and budget < 1:
        raise ValueError("budget must be positive or None")
    day = _days(frame)
    keys = pd.DataFrame(
        {
            "day": day.to_numpy(),
            "object_id": frame["object_id"].to_numpy(),
            "system_type": frame["system_type"].to_numpy(),
        }
    )
    if keys.duplicated().any():
        raise ValueError("rows must be unique per object_id × system_type × day")
    score = frame[score_col].to_numpy(dtype=float)
    eligible = np.isfinite(score)
    if threshold is not None:
        eligible &= score >= threshold
    keys["score"] = score
    keys["position"] = np.arange(len(frame))
    ranked = keys[eligible].sort_values(
        ["day", "score", "object_id", "system_type"],
        ascending=[True, False, True, True],
        kind="mergesort",
    )
    keep = np.ones(len(ranked), dtype=bool)
    if budget is not None:
        keep = ranked.groupby("day", sort=False).cumcount().to_numpy() < budget
    mask = np.zeros(len(frame), dtype=bool)
    mask[ranked["position"].to_numpy()[keep]] = True
    return mask


def _confusion(y: np.ndarray, cards: np.ndarray) -> dict:
    tp = int(np.sum(cards & (y == 1)))
    fp = int(np.sum(cards & (y == 0)))
    fn = int(np.sum(~cards & (y == 1)))
    tn = int(np.sum(~cards & (y == 0)))
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    denominator = 2 * tp + fp + fn
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": 2 * tp / denominator if denominator else None,
    }


def select_threshold(frame: pd.DataFrame, score_col: str, *, budget: int | None = 10) -> dict:
    """Threshold maximizing F1 of the budget-capped card policy.

    For any threshold the cards are the day's top-``budget`` rows with score >=
    threshold, so candidate thresholds are the unique scores of top-``budget``
    rows (other unique scores give the same card set). Ties in F1 choose the
    higher threshold (fewer cards).
    """
    rows = _evaluable(frame, [score_col])
    y = _labels(rows)
    positives = int(y.sum())
    top = card_mask(rows, score_col, budget=budget)
    if not top.any():
        raise ValueError("no scored evaluable rows")
    score = rows[score_col].to_numpy(dtype=float)[top]
    hit = y[top]
    order = np.argsort(-score, kind="mergesort")
    score, hit = score[order], hit[order]
    last = np.r_[score[1:] != score[:-1], True]
    cards = np.arange(1, len(score) + 1)[last]
    tp = np.cumsum(hit)[last]
    f1 = np.where(cards + positives > 0, 2 * tp / (cards + positives), 0.0)
    best = int(np.argmax(f1))
    return _py(
        {
            "threshold": score[last][best],
            "f1": f1[best],
            "precision": tp[best] / cards[best],
            "recall": tp[best] / positives if positives else None,
            "cards": cards[best],
            "tp": tp[best],
            "fp": cards[best] - tp[best],
            "fn": positives - tp[best],
            "positives": positives,
            "candidate_thresholds": int(last.sum()),
            "budget": budget,
            "rule": "max F1 of budget-capped cards over unique top-budget scores; ties -> higher",
        }
    )


def _pr_auc(y: np.ndarray, score: np.ndarray) -> float | None:
    if y.min(initial=1) == y.max(initial=0):
        return None
    return float(average_precision_score(y, score))


def _load(days: pd.Series, cards: np.ndarray, budget: int | None) -> dict:
    per_day = pd.Series(cards.astype(int)).groupby(days.to_numpy()).sum()
    values = per_day.to_numpy()
    if not len(values):
        return {"days": 0}
    return {
        "days": len(values),
        "cards_total": int(values.sum()),
        "cards_per_day_mean": float(values.mean()),
        "cards_per_day_p95": float(np.percentile(values, 95)),
        "cards_per_day_max": int(values.max()),
        "days_with_cards": int((values > 0).sum()),
        "days_at_budget": int((values >= budget).sum()) if budget is not None else None,
    }


def _objects(rows: pd.DataFrame, y: np.ndarray, cards: np.ndarray) -> dict:
    stats = (
        pd.DataFrame(
            {
                "object_id": rows["object_id"].to_numpy(),
                "pos": y,
                "tp": cards & (y == 1),
                "card": cards,
            }
        )
        .groupby("object_id")
        .sum()
    )
    with_pos = stats[stats["pos"] > 0]
    tp_total = int(stats["tp"].sum())
    top_tp = np.sort(stats["tp"].to_numpy())[::-1]
    return {
        "objects": len(stats),
        "objects_with_positives": len(with_pos),
        "objects_with_cards": int((stats["card"] > 0).sum()),
        "objects_with_tp": int((stats["tp"] > 0).sum()),
        "macro_recall_over_objects": (
            float((with_pos["tp"] / with_pos["pos"]).mean()) if len(with_pos) else None
        ),
        "tp_share_top1_object": float(top_tp[:1].sum() / tp_total) if tp_total else None,
        "tp_share_top3_objects": float(top_tp[:3].sum() / tp_total) if tp_total else None,
    }


def _bucket_labels(edges: list[int]) -> list[str]:
    labels = []
    for low, high in zip(edges, [*edges[1:], None], strict=True):
        if high is None:
            labels.append(f"{low}+")
        elif high - low == 1:
            labels.append(str(low))
        else:
            labels.append(f"{low}-{high - 1}")
    return labels


def _size_buckets(
    rows: pd.DataFrame, y: np.ndarray, cards: np.ndarray, score: np.ndarray, edges: list[int]
) -> dict:
    known = rows["known_channels"].to_numpy(dtype=float)
    labels = _bucket_labels(edges)
    index = np.searchsorted(np.asarray(edges, dtype=float), known, side="right") - 1
    result = {}
    if (index < 0).any():
        labels = [f"<{edges[0]}", *labels]
        index = index + 1
    elif edges:
        index = index.clip(min=0)
    for i, label in enumerate(labels):
        part = index == i
        result[label] = {
            "rows": int(part.sum()),
            "positives": int(y[part].sum()),
            **_confusion(y[part], cards[part]),
            "pr_auc": _pr_auc(y[part], score[part]) if part.any() else None,
        }
    return result


def _strata(rows: pd.DataFrame, y: np.ndarray, cards: np.ndarray) -> dict:
    masks = {name: (rows["stratum"] == name).to_numpy() & (y == 1) for name in STRATA}
    masks["new_channel_alarm"] = rows["new_channel_alarm"].fillna(False).to_numpy(dtype=bool) & (
        y == 1
    )
    result = {}
    for name, part in masks.items():
        tp = int(np.sum(cards & part))
        positives = int(part.sum())
        result[name] = {
            "positives": positives,
            "tp": tp,
            "recall": tp / positives if positives else None,
        }
    return result


def _episodes(rows: pd.DataFrame, cards: np.ndarray) -> dict:
    start_col = "episode_start_at" if "episode_start_at" in rows else "first_alarm_at"
    starts = (
        rows["episode_start_in_window"].fillna(False).to_numpy(dtype=bool)
        & rows["episode_id"].notna().to_numpy()
    )
    part = pd.DataFrame(
        {
            "episode_id": rows["episode_id"].to_numpy()[starts],
            "start": pd.to_datetime(rows[start_col]).to_numpy()[starts],
            "issued_at": pd.to_datetime(rows["issued_at"]).to_numpy()[starts],
            "card": cards[starts],
        }
    )
    episodes = part["episode_id"].nunique()
    captured = part[part["card"]]
    first = captured.groupby("episode_id").agg(start=("start", "min"), issued=("issued_at", "min"))
    lead = ((first["start"] - first["issued"]).dt.total_seconds() / 3600).to_numpy()
    quantiles = np.percentile(lead, [25, 50, 75]) if len(lead) else [None] * 3
    return {
        "episodes": int(episodes),
        "captured": len(first),
        "capture_rate": len(first) / episodes if episodes else None,
        "lead_hours_p25": quantiles[0],
        "lead_hours_median": quantiles[1],
        "lead_hours_p75": quantiles[2],
    }


def _policy(
    rows: pd.DataFrame,
    y: np.ndarray,
    cards: np.ndarray,
    score: np.ndarray,
    budget: int | None,
    config: dict,
) -> dict:
    systems = {}
    names = list(config["system_types"])
    names += sorted(set(rows["system_type"].dropna()) - set(names))
    for name in names:
        part = (rows["system_type"] == name).to_numpy()
        systems[config["system_types"].get(name, name)] = {
            "system_type": name,
            "rows": int(part.sum()),
            "positives": int(y[part].sum()),
            "base_rate": float(y[part].mean()) if part.any() else None,
            "cards": int(cards[part].sum()),
            **_confusion(y[part], cards[part]),
            "pr_auc": _pr_auc(y[part], score[part]) if part.any() else None,
        }

    def mean_of(key: str, keep) -> dict:
        values = [s[key] for s in systems.values() if keep(s) and s[key] is not None]
        return {"value": float(np.mean(values)) if values else None, "systems": len(values)}

    with_pos = lambda s: s["positives"] > 0  # noqa: E731
    macro = {
        "recall": mean_of("recall", with_pos),
        "precision": mean_of("precision", lambda s: s["cards"] > 0),
        "f1": mean_of("f1", with_pos),
        "pr_auc": mean_of("pr_auc", lambda s: True),
        "rule": "recall/f1 over systems with positives, precision over systems with cards, "
        "pr_auc over systems with both classes",
    }
    return {
        **_confusion(y, cards),
        "cards": int(cards.sum()),
        "load": _load(_days(rows), cards, budget),
        "by_system": systems,
        "macro_over_systems": macro,
        "objects": _objects(rows, y, cards),
        "size_buckets": _size_buckets(
            rows, y, cards, score, list(config["evaluation"]["size_buckets_channels"])
        ),
        "strata": _strata(rows, y, cards),
        "episodes": _episodes(rows, cards),
    }


def calibration(y: np.ndarray, score: np.ndarray, *, bins: int = 10) -> dict:
    if ((score < 0) | (score > 1)).any():
        raise ValueError("probability scores must lie in [0, 1]")
    index = np.minimum((score * bins).astype(int), bins - 1)
    table, ece = [], 0.0
    for b in range(bins):
        part = index == b
        n = int(part.sum())
        row = {
            "bin_low": b / bins,
            "bin_high": (b + 1) / bins,
            "rows": n,
            "mean_score": None,
            "observed_rate": None,
        }
        if n:
            row["mean_score"] = float(score[part].mean())
            row["observed_rate"] = float(y[part].mean())
            ece += n / len(y) * abs(row["observed_rate"] - row["mean_score"])
        table.append(row)
    return {
        "brier": float(np.mean((score - y) ** 2)),
        "ece": ece,
        "bins": bins,
        "reliability": table,
    }


def evaluate_scores(
    frame: pd.DataFrame,
    score_col: str,
    *,
    threshold: float,
    budget: int | None = 10,
    probability: bool = True,
    top_k: int | None = None,
) -> dict:
    """Threshold and top-k card policies plus ranking, calibration and breakdowns."""
    config = _config()
    top_k = top_k or int(config["evaluation"]["top_k_comparison"])
    excluded = frame[frame["outcome"] == "excluded"]
    rows = _evaluable(frame, [score_col])
    y = _labels(rows)
    score = rows[score_col].to_numpy(dtype=float)
    threshold_cards = card_mask(rows, score_col, budget=budget, threshold=threshold)
    top_cards = card_mask(rows, score_col, budget=top_k)
    reasons = excluded["exclusion_reason"].fillna("missing").value_counts().sort_index()
    result = {
        "score": score_col,
        "rows_total": len(frame),
        "rows_evaluable": len(rows),
        "rows_excluded": len(excluded),
        "excluded_by_reason": reasons.to_dict(),
        "positives": int(y.sum()),
        "base_rate": float(y.mean()) if len(y) else None,
        "pr_auc": _pr_auc(y, score),
        "threshold": threshold,
        "budget": budget,
        "top_k": top_k,
        "threshold_policy": _policy(rows, y, threshold_cards, score, budget, config),
        "topk_policy": _policy(rows, y, top_cards, score, top_k, config),
    }
    if probability:
        result["calibration"] = calibration(y, score)
    return _py(result)


def weighted_average_precision(y: np.ndarray, score: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Average precision (sklearn definition) for each row of a weight matrix."""
    weights = np.atleast_2d(weights).astype(float)
    order = np.argsort(-score, kind="mergesort")
    y, score, weights = y[order], score[order], weights[:, order]
    ends = np.r_[score[1:] != score[:-1], True]
    tp = np.cumsum(weights * y, axis=1)[:, ends]
    fp = np.cumsum(weights * (1 - y), axis=1)[:, ends]
    positives = tp[:, -1:]
    with np.errstate(invalid="ignore", divide="ignore"):
        precision = np.where(tp + fp > 0, tp / (tp + fp), 0.0)
        recall = tp / positives
    step = np.diff(np.concatenate([np.zeros((len(recall), 1)), recall], axis=1), axis=1)
    ap = np.sum(step * precision, axis=1)
    return np.where(positives[:, 0] > 0, ap, np.nan)


def _interval(point, values: np.ndarray) -> dict:
    valid = values[np.isfinite(values)]
    return {
        "point": point,
        "low": float(np.percentile(valid, 2.5)) if len(valid) else None,
        "high": float(np.percentile(valid, 97.5)) if len(valid) else None,
        "undefined_replicates": int(len(values) - len(valid)),
    }


def _block_codes(rows: pd.DataFrame, scheme: str) -> np.ndarray:
    if scheme == "iso_week":
        iso = pd.to_datetime(rows["issued_at"]).dt.isocalendar()
        key = iso["year"].astype(str) + "-" + iso["week"].astype(str).str.zfill(2)
    elif scheme == "object_id":
        key = rows["object_id"]
    else:
        raise ValueError(f"unknown block scheme: {scheme}")
    return pd.factorize(key, sort=True)[0]


def bootstrap_intervals(
    frame: pd.DataFrame,
    score_cols: list[str],
    *,
    thresholds: dict,
    budget: int = 10,
    replicates: int = 400,
    seed: int = 17,
    deltas: list | tuple = (),
    schemes: tuple[str, ...] = BLOCK_SCHEMES,
) -> dict:
    """Block-bootstrap 95% percentile intervals with cards fixed on the full period.

    Each replicate resamples ISO weeks or objects with replacement and reweights
    rows; all score columns share the same replicate, so deltas ``(a, b, name)``
    are paired (a − b). A threshold of ``None`` means the top-``budget`` policy.
    """
    rows = _evaluable(frame, list(score_cols))
    y = _labels(rows).astype(float)
    for a, b, _ in deltas:
        if a not in score_cols or b not in score_cols:
            raise ValueError(f"delta columns must be among score_cols: {a}, {b}")
    cards = {
        col: card_mask(rows, col, budget=budget, threshold=thresholds[col]).astype(float)
        for col in score_cols
    }
    scores = {col: rows[col].to_numpy(dtype=float) for col in score_cols}

    def metrics(weights: np.ndarray) -> dict:
        out = {}
        pos = weights @ y
        for col in score_cols:
            tp = weights @ (cards[col] * y)
            n_cards = weights @ cards[col]
            with np.errstate(invalid="ignore", divide="ignore"):
                out[col] = {
                    "precision": np.where(n_cards > 0, tp / n_cards, np.nan),
                    "recall": np.where(pos > 0, tp / pos, np.nan),
                    "pr_auc": weighted_average_precision(y, scores[col], weights),
                }
        return out

    point = metrics(np.ones((1, len(rows))))
    result = {
        "replicates": replicates,
        "seed": seed,
        "budget": budget,
        "thresholds": thresholds,
        "cards": "fixed on the full period; blocks resampled with replacement",
        "schemes": {},
    }
    for offset, scheme in enumerate(schemes):
        codes = _block_codes(rows, scheme)
        blocks = int(codes.max()) + 1 if len(codes) else 0
        rng = np.random.default_rng(seed + offset)
        draws = rng.integers(0, blocks, size=(replicates, blocks))
        collected = {col: {m: [] for m in ("precision", "recall", "pr_auc")} for col in score_cols}
        for lo in range(0, replicates, _BATCH):
            counts = np.stack(
                [np.bincount(d, minlength=blocks) for d in draws[lo : lo + _BATCH]]
            ).astype(float)
            batch = metrics(counts[:, codes])
            for col in score_cols:
                for m in collected[col]:
                    collected[col][m].append(batch[col][m])
        values = {
            col: {m: np.concatenate(v) for m, v in per.items()} for col, per in collected.items()
        }
        block = {
            "blocks": blocks,
            "scores": {
                col: {m: _interval(float(point[col][m][0]), values[col][m]) for m in values[col]}
                for col in score_cols
            },
            "deltas": {},
        }
        for a, b, name in deltas:
            block["deltas"][name] = {"a": a, "b": b}
            for m in ("precision", "recall", "pr_auc"):
                diff = values[a][m] - values[b][m]
                entry = _interval(float(point[a][m][0] - point[b][m][0]), diff)
                valid = diff[np.isfinite(diff)]
                entry["share_positive"] = float((valid > 0).mean()) if len(valid) else None
                block["deltas"][name][m] = entry
        result["schemes"][scheme] = block
    return _py(result)

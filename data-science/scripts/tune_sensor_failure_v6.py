"""``sensor-failure-models-v6`` runner (pre-registered by the data analyst on 27.09.2026 and frozen
before the first fit): do other model families beat the statistical model, the static
list of last year, at 7 and 14 days? Development folds only (evaluation years 2023 and
2024); the calibration period and the holdout are never read.

- ``stats`` — candidates without gradient training, per fold: the Bayesian Poisson-gamma
  frequency of the pair (candidate 5) and the Hawkes-like intensity of the pair
  (candidate 7, maximum likelihood per sensor type on the fit years); card scores for
  7 and 14 days; MLflow runs with the parameters.
- ``fit --horizon H`` — LightGBM, XGBoost, CatBoost YetiRank and MLP (candidates 1-4),
  seeds 17/18/19 with the pre-registered abort rule, both folds; MLflow runs with their
  models; card scores (mean of the top two candidate channels) go to gitignored
  ``artifacts/sensor-failure-v1/tuning_v6/<H>h/``.
- ``report`` — stacking (candidate 6), the criterion "better than statistics" per horizon
  and K with the paired week/object bootstrap, the fresh-pair slice at 7 days, the lead
  and calibration of the Hawkes candidate (technologist criterion K2), reference runs in
  MLflow. Aggregates only, no identifiers.

    python data-science/scripts/tune_sensor_failure_v6.py --stage stats
    python data-science/scripts/tune_sensor_failure_v6.py --stage fit --horizon 168
    python data-science/scripts/tune_sensor_failure_v6.py --stage fit --horizon 336
    python data-science/scripts/tune_sensor_failure_v6.py --stage report
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import subprocess
from importlib.metadata import version
from math import log
from pathlib import Path
from time import perf_counter

import duckdb
import numpy as np
import pandas as pd

from infra_pulse_research.modeling import sensor_failure_evaluation as ev
from infra_pulse_research.modeling.sensor_failure_modeling import (
    fit_weights,
    matrix,
    negative_sample_mask,
    split_kinds,
)
from infra_pulse_research.modeling.sensor_failure_nodes import select_rolling_release
from infra_pulse_research.modeling.sensor_failure_tuning import card_aggregates, day_rank
from infra_pulse_research.modeling.subsystem_modeling import categorical_frame, numeric_frame
from infra_pulse_research.modeling.target_audit import wilson_interval

HERE = Path(__file__).resolve().parent
V5 = runpy.run_path(str(HERE / "tune_sensor_failure_v5.py"), run_name="v5")
ROOT, SF, LABELS, V5_OUT = V5["ROOT"], V5["SF"], V5["LABELS"], V5["OUT"]
CONFIG = ROOT / "data-science/configs/sensor_failure_tuning_v6.json"
OUT = SF / "tuning_v6"
REPORT = ROOT / "data-science/reports/sensor-failure-models-v6-2026-09-27.json"
EXPLORATION3 = ROOT / "data-science/reports/sensor-failure-v5-exploration3-2026-09-26.json"
EXPLORATION4 = ROOT / "data-science/reports/sensor-failure-v5-exploration4-2026-09-27.json"
TARGET, RULE = V5["THRESHOLDS"]["ge2s"]
KEYS, DEV_END, DEV_MONTHS = V5["KEYS"], V5["DEV_END"], V5["DEV_MONTHS"]
UNIT = ["object_id", "sensor_type"]
LABEL = "connection_loss"
HORIZONS = {168: 7, 336: 14}
LIST, PERSIST, CEIL = "static_list", "persistence", "ceiling"
FAMILIES = ("LightGBM", "XGBoost", "CatBoost YetiRank", "MLP")
BAYES, HAWKES = "Bayes PG", "Hawkes"
F2V5 = "F2 CatBoost v5"
EXCLUDED_YEAR = 2021
ORIGIN = pd.Timestamp("2019-01-01")
DAY = pd.Timedelta(1, "D")
GAP_DAYS = (
    float((pd.Timestamp("2021-01-01") - ORIGIN) / DAY),
    float((pd.Timestamp("2022-01-01") - ORIGIN) / DAY),
)
MIN_TYPE_EVENTS = 30
MIN_PRIOR_DAYS = 30.0
HAWKES_STARTS = (0.5, 5.0, 50.0, 500.0)
HAWKES_BOUNDS = ((1e-7, 10.0), (1e-7, 100.0), (0.05, 3650.0))
PROB_EDGES = (0.0, 0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0)
FRESH_HORIZON, FRESH_BUDGETS = 168, (2, 3)
REFERENCE_RUNS = {
    LIST: ("static list (правило)", "Правило без обучения: число событий пары за 365 сут."),
    PERSIST: ("persistence (правило)", "Правило без обучения: давность последнего кандидата."),
    F2V5: (
        "F2 CatBoost v5 (сохранённые оценки)",
        "Сохранённые оценки CatBoost F2 v5 (7 сут — seed 17/18/19, 14 сут — seed 17); "
        "не переобучалась.",
    ),
    CEIL: ("ORACLE ceiling (не модель)", "Жадный оракул, знающий будущие события; не модель."),
}


def _q(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _files(paths) -> str:
    return "[" + ", ".join(_q(p) for p in paths) + "]"


def _sha(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def year_months(year: int) -> list[str]:
    return [f"{year}-{m:02d}" for m in range(1, 13)]


def fold_year(fold: dict) -> int:
    return int(fold["evaluate"][0][0][:4])


def _con(out: Path = OUT) -> duckdb.DuckDBPyConnection:
    """DuckDB with its own spill directory (parallel processes must not share one)."""
    tmp = out / "tmp" / str(os.getpid())
    tmp.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET threads=6; SET memory_limit='6GB'; SET temp_directory={_q(tmp)}")
    return con


def policy(days: int, k: int) -> dict:
    return {"type": "rolling_release", "max_open": int(k), "window_days": int(days)}


def select(cards: pd.DataFrame, score: str, days: int, k: int, eligible=None) -> np.ndarray:
    """Rolling list with release; ``eligible`` restricts the list to some cards."""
    if eligible is not None:
        cards = cards.assign(_eligible_score=cards[score].where(eligible))
        score = "_eligible_score"
    return select_rolling_release(cards, score, policy(days, k))


# -- history of a pair (candidates 5 and 7) ----------------------------------------------


def pair_event_times(members: pd.DataFrame, rule, *, drop_year: int | None = EXCLUDED_YEAR):
    """Per event and pair object x sensor type, the moment the event counts for the pair,
    as ``static_list_scores``: the later of the pair's first member start and the moment
    the event is known to qualify. Events of members starting in ``drop_year`` are left
    out (2021 is outside the project's training data)."""
    m = members.copy()
    m["start_at"] = pd.to_datetime(m["start_at"])
    if drop_year is not None:
        m = m[m["start_at"].dt.year != drop_year]
    known = ev._known_after(m["start_at"], rule).where(m["qualifying"].astype(bool))
    event_known = known.groupby(m["event_id"]).min().rename("event_known")
    first = m.groupby(["event_id", *UNIT])["start_at"].min().rename("first").reset_index()
    first = first.merge(event_known, left_on="event_id", right_index=True, how="left")
    first = first.dropna(subset=["event_known"])
    first["count_at"] = first[["first", "event_known"]].max(axis=1)
    return first[["event_id", *UNIT, "count_at"]].reset_index(drop=True)


def to_days(values) -> np.ndarray:
    return ((pd.to_datetime(pd.Series(values)) - ORIGIN) / DAY).to_numpy(dtype=float)


def observed_parts(start: float, end: float) -> list[tuple[float, float]]:
    """``[start, end)`` in days since 2019-01-01 without the excluded year 2021."""
    parts = []
    for lo, hi in ((-np.inf, GAP_DAYS[0]), (GAP_DAYS[1], np.inf)):
        a, b = max(start, lo), min(end, hi)
        if b > a:
            parts.append((a, b))
    return parts


def decayed_sum(event_days: np.ndarray, t: np.ndarray, rate: float) -> np.ndarray:
    """Sum over events strictly before each t of exp(-rate * (t - event))."""
    t = np.asarray(t, dtype=float)
    if not len(event_days):
        return np.zeros(len(t))
    age = t[:, None] - np.asarray(event_days, dtype=float)[None, :]
    past = age > 0
    return np.where(past, np.exp(-rate * np.where(past, age, 0.0)), 0.0).sum(axis=1)


def decayed_exposure(start: float, t: np.ndarray, rate: float) -> np.ndarray:
    """Observed time of a pair in ``[start, t)`` without 2021, weighted by
    exp(-rate * (t - s)); in days (plain length for rate 0)."""
    t = np.asarray(t, dtype=float)
    out = np.zeros(len(t))
    for lo, hi in ((-np.inf, GAP_DAYS[0]), (GAP_DAYS[1], np.inf)):
        a = max(start, lo)
        b = np.minimum(t, hi)
        ok = b > a
        if rate > 0:
            value = (np.exp(-rate * (t - np.where(ok, b, t))) - np.exp(-rate * (t - a))) / rate
        else:
            value = b - a
        out += np.where(ok, value, 0.0)
    return out


def pair_starts(con) -> pd.DataFrame:
    """First card of every pair in the development months (the pair is observed from it)."""
    files = _files(LABELS / f"cards/{TARGET}/168h/{m}.parquet" for m in DEV_MONTHS)
    frame = con.execute(
        f"SELECT object_id, sensor_type, min(issued_at) AS first_card FROM read_parquet({files}) "
        f"WHERE issued_at < TIMESTAMP {_q(DEV_END)} GROUP BY 1, 2 ORDER BY 1, 2"
    ).df()
    frame["start"] = to_days(frame["first_card"])
    return frame


def pair_histories(times: pd.DataFrame, starts: pd.DataFrame, end) -> list[dict]:
    """Per pair seen before ``end``: its events before ``end`` (days, sorted), the observed
    parts of ``[first card, end)`` without 2021, the observed exposure and event count."""
    end_d = float(to_days([end])[0])
    days = times.assign(_d=to_days(times["count_at"]))
    days = days[days["_d"] < end_d]
    grouped = {k: np.sort(g["_d"].to_numpy()) for k, g in days.groupby(UNIT, sort=False)}
    out = []
    for obj, sensor, start in zip(
        starts["object_id"], starts["sensor_type"], starts["start"], strict=True
    ):
        if start >= end_d:
            continue
        events = grouped.get((obj, sensor), np.array([]))
        parts = observed_parts(float(start), end_d)
        inside = np.zeros(len(events), dtype=bool)
        for a, b in parts:
            inside |= (events >= a) & (events < b)
        out.append(
            {
                "sensor_type": sensor,
                "events": events,
                "parts": parts,
                "observed": inside,
                "exposure": float(sum(b - a for a, b in parts)),
                "n": int(inside.sum()),
            }
        )
    return out


# -- candidate 5: Bayesian Poisson-gamma -------------------------------------------------


def gamma_prior(counts: np.ndarray, exposures: np.ndarray) -> tuple[float, float] | None:
    """Method of moments for a gamma prior of Poisson rates with unequal exposures:
    m = sum n / sum E, v = var(n / E) - m * mean(1 / E); alpha = m^2 / v, beta = m / v."""
    keep = np.asarray(exposures, dtype=float) >= MIN_PRIOR_DAYS
    n = np.asarray(counts, dtype=float)[keep]
    e = np.asarray(exposures, dtype=float)[keep]
    if len(n) < 3 or n.sum() <= 0:
        return None
    m = n.sum() / e.sum()
    v = float(np.var(n / e, ddof=1) - m * np.mean(1.0 / e))
    if not v > 0:
        return None
    return m * m / v, m / v


def fit_bayes(histories: list[dict]) -> dict:
    """Gamma prior per sensor type (method of moments), pooled fallback."""
    counts = np.array([h["n"] for h in histories], dtype=float)
    exposure = np.array([h["exposure"] for h in histories], dtype=float)
    types = np.array([h["sensor_type"] for h in histories], dtype=object)
    pooled = gamma_prior(counts, exposure)
    source = "pooled"
    if pooled is None:
        m = counts.sum() / exposure.sum()
        pooled, source = (1.0, 1.0 / m), "exponential prior with the pooled mean"
    params = {"_pooled": {"alpha": pooled[0], "beta": pooled[1], "source": source}}
    for sensor in sorted(set(types)):
        own = types == sensor
        prior = gamma_prior(counts[own], exposure[own])
        params[str(sensor)] = {
            "alpha": (prior or pooled)[0],
            "beta": (prior or pooled)[1],
            "source": "type" if prior is not None else "pooled",
            "pairs": int(own.sum()),
            "events": int(counts[own].sum()),
            "exposure_days": float(exposure[own].sum()),
        }
    return params


def bayes_scores(
    cards: pd.DataFrame, times: pd.DataFrame, starts: pd.DataFrame, params: dict, days: int
) -> np.ndarray:
    """P(at least one event in [t, t + D)) under the gamma posterior with decayed counts
    and decayed exposure (half-life ``params['_half_life']``); only events before t."""
    rate = log(2) / float(params["_half_life"])
    events = {k: np.sort(to_days(g["count_at"])) for k, g in times.groupby(UNIT, sort=False)}
    first = {
        (o, s): v
        for o, s, v in zip(starts.object_id, starts.sensor_type, starts.start, strict=True)
    }
    issued = to_days(cards["issued_at"])
    out = np.full(len(cards), np.nan)
    for key, positions in cards.groupby(UNIT, sort=False).indices.items():
        t = issued[positions]
        prior = params.get(str(key[1]), params["_pooled"])
        a = prior["alpha"] + decayed_sum(events.get(key, np.array([])), t, rate)
        b = prior["beta"] + decayed_exposure(first.get(key, float(t.min())), t, rate)
        out[positions] = -np.expm1(a * np.log(b / (b + days)))
    return out


# -- candidate 7: Hawkes-like intensity --------------------------------------------------


def hawkes_data(histories: list[dict]) -> dict:
    """Arrays of one group of pairs for the likelihood: events sorted by pair and time,
    the gap to the previous event of the pair (inf for the first), whether the event lies
    in observed time, and the observed parts after each event (two slots, padded)."""
    times, gaps, observed, lo, hi = [], [], [], [], []
    exposure = 0.0
    for h in histories:
        e = h["events"]
        exposure += h["exposure"]
        if not len(e):
            continue
        times.append(e)
        gaps.append(np.diff(e, prepend=-np.inf))
        observed.append(h["observed"])
        slots_lo = np.repeat(e[:, None], 2, axis=1)
        slots_hi = slots_lo.copy()
        for j, (a, b) in enumerate(h["parts"]):
            after = b > e
            slots_lo[after, j] = np.maximum(a, e[after])
            slots_hi[after, j] = b
        lo.append(slots_lo)
        hi.append(slots_hi)
    if not times:
        empty = np.zeros(0)
        return {
            "e": empty,
            "gap": empty,
            "obs": empty.astype(bool),
            "lo": np.zeros((0, 2)),
            "hi": np.zeros((0, 2)),
            "exposure": exposure,
            "pairs": len(histories),
        }
    e = np.concatenate(times)
    return {
        "e": e,
        "gap": np.concatenate(gaps),
        "obs": np.concatenate(observed),
        "lo": np.concatenate(lo),
        "hi": np.concatenate(hi),
        "exposure": exposure,
        "pairs": len(histories),
    }


def excitation(decay: np.ndarray, first: np.ndarray) -> np.ndarray:
    """A_i = sum over earlier events j of the pair of exp(-(t_i - t_j) / tau), from the
    recursion A_i = decay_i * (1 + A_{i-1}) with A = 0 at the first event of a pair."""
    out = np.zeros(len(decay))
    acc = 0.0
    for i in range(len(decay)):
        acc = 0.0 if first[i] else decay[i] * (1.0 + acc)
        out[i] = acc
    return out


def hawkes_loglik(mu: float, alpha: float, tau: float, data: dict) -> float:
    """Log-likelihood of the observed events of a group of pairs: sum of log intensities
    at observed events minus the integral of the intensity over observed time."""
    if not len(data["e"]):
        return -mu * data["exposure"]
    first = ~np.isfinite(data["gap"])
    decay = np.exp(-np.where(first, 0.0, data["gap"]) / tau)
    a = excitation(decay, first)
    lam = mu + alpha * a[data["obs"]]
    e = data["e"][:, None]
    kernel = tau * (np.exp(-(data["lo"] - e) / tau) - np.exp(-(data["hi"] - e) / tau))
    return float(np.log(lam).sum() - mu * data["exposure"] - alpha * kernel.sum())


def fit_hawkes_group(data: dict) -> dict:
    from scipy.optimize import minimize  # noqa: PLC0415

    n_obs = int(data["obs"].sum())
    base = max(n_obs / max(data["exposure"], 1e-9), 1e-6)
    bounds = [(np.log(a), np.log(b)) for a, b in HAWKES_BOUNDS]

    def nll(x):
        mu, alpha, tau = np.exp(x)
        return -hawkes_loglik(mu, alpha, tau, data)

    best = None
    for tau0 in HAWKES_STARTS:
        x0 = np.log([0.5 * base, 0.3 / tau0, tau0])
        x0 = np.clip(x0, [b[0] for b in bounds], [b[1] for b in bounds])
        res = minimize(nll, x0, method="L-BFGS-B", bounds=bounds)
        if best is None or res.fun < best.fun:
            best = res
    mu, alpha, tau = (float(v) for v in np.exp(best.x))
    return {
        "mu": mu,
        "alpha": alpha,
        "tau": tau,
        "branching": alpha * tau,
        "loglik": float(-best.fun),
        "converged": bool(best.success),
        "at_bound": [
            bool(np.isclose(x, lo) or np.isclose(x, hi))
            for x, (lo, hi) in zip(best.x, bounds, strict=True)
        ],
        "events": n_obs,
        "pairs": data["pairs"],
        "exposure_days": float(data["exposure"]),
    }


def fit_hawkes(histories: list[dict]) -> dict:
    """mu, alpha, tau per sensor type by maximum likelihood; types with fewer than
    ``MIN_TYPE_EVENTS`` observed events use the parameters of all types pooled."""
    pooled = fit_hawkes_group(hawkes_data(histories))
    params = {"_pooled": {**pooled, "source": "pooled"}}
    for sensor in sorted({h["sensor_type"] for h in histories}):
        own = [h for h in histories if h["sensor_type"] == sensor]
        events = sum(h["n"] for h in own)
        if events >= MIN_TYPE_EVENTS:
            params[str(sensor)] = {**fit_hawkes_group(hawkes_data(own)), "source": "type"}
        else:
            params[str(sensor)] = {
                **{k: pooled[k] for k in ("mu", "alpha", "tau", "branching")},
                "source": "pooled",
                "events": events,
                "pairs": len(own),
            }
    return params


def hawkes_integral(mu: float, alpha: float, tau: float, excite: np.ndarray, days: int):
    """Integral of the intensity over [t, t + D) before any event inside the window."""
    return mu * days + alpha * tau * (1.0 - np.exp(-days / tau)) * excite


def hawkes_scores(cards: pd.DataFrame, times: pd.DataFrame, params: dict, days: int):
    """P(at least one event in [t, t + D)) = 1 - exp(-integral), events known before t."""
    events = {k: np.sort(to_days(g["count_at"])) for k, g in times.groupby(UNIT, sort=False)}
    issued = to_days(cards["issued_at"])
    out = np.full(len(cards), np.nan)
    for key, positions in cards.groupby(UNIT, sort=False).indices.items():
        p = params.get(str(key[1]), params["_pooled"])
        excite = decayed_sum(events.get(key, np.array([])), issued[positions], 1.0 / p["tau"])
        out[positions] = -np.expm1(-hawkes_integral(p["mu"], p["alpha"], p["tau"], excite, days))
    return out


# -- saved cards -------------------------------------------------------------------------


def v5_f2_columns(horizon: int) -> dict[str, str]:
    """Saved v5 CatBoost F2 card scores: 7 days seeds 17/18/19, 14 days seed 17."""
    if horizon == 168:
        return {f"F2 s{s}": f"{F2V5} s{s}" for s in (17, 18, 19)}
    return {"F2": f"{F2V5} s17"}


def load_cards(horizon: int, index: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """v5 cards of the evaluation year with the list, persistence and F2 v5 scores."""
    folder = V5_OUT / f"{horizon}h"
    cards = pd.read_parquet(folder / f"cards_fold{index}.parquet")
    links = pd.read_parquet(folder / f"links_fold{index}.parquet")
    rename = v5_f2_columns(horizon)
    drop = [c for c in cards.columns if c.startswith("F2") and c not in rename]
    return cards.drop(columns=drop).rename(columns=rename), links


# -- ML families (candidates 1-4) --------------------------------------------------------


def within_day_ranks(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Percentile rank (average ties, 1 = highest) of each column among the rows of the
    same ``issued_at``; missing values stay missing."""
    values = numeric_frame(frame, columns)
    ranks = values.groupby(frame["issued_at"].to_numpy()).rank(pct=True, method="average")
    return ranks.astype("float32")


class Inputs:
    """Column kinds and categories from the fit sample, and the matrices per family."""

    def __init__(self, sample: pd.DataFrame, columns: list[str], ranks: pd.DataFrame):
        self.columns = columns
        self.numeric, self.categorical = split_kinds(columns, sample)
        cats = categorical_frame(sample, self.categorical)
        self.categories = {c: sorted(cats[c].unique()) for c in self.categorical}
        self.missing = [c for c in self.numeric if ranks[c].isna().any()]

    def trees(self, frame: pd.DataFrame) -> pd.DataFrame:
        x = numeric_frame(frame, self.numeric)
        cats = categorical_frame(frame, self.categorical)
        for c in self.categorical:
            x[c] = pd.Categorical(cats[c], categories=self.categories[c])
        return x

    def catboost(self, frame: pd.DataFrame) -> pd.DataFrame:
        return matrix(frame, self.numeric, self.categorical)

    def mlp(self, frame: pd.DataFrame, ranks: pd.DataFrame) -> np.ndarray:
        parts = [
            ranks[self.numeric].fillna(0.0).to_numpy(dtype=np.float32),
            ranks[self.missing].isna().to_numpy(dtype=np.float32),
        ]
        cats = categorical_frame(frame, self.categorical)
        for c in self.categorical:
            known = np.asarray(self.categories[c], dtype=object)
            parts.append((cats[c].to_numpy(dtype=object)[:, None] == known[None, :]).astype("f4"))
        return np.hstack(parts)


def family_params(config: dict, family: str) -> dict:
    spec = next(c for c in config["candidates"].values() if c["family"] == family)
    return {k: v for k, v in spec["params"].items() if k not in ("config", "estimator", "other")}


def fit_family(family: str, params: dict, sample, y, groups, ranks, inputs: Inputs, seed: int):
    """One pre-registered model; returns (model, info)."""
    weights = fit_weights(y)
    info: dict = {}
    if family == "LightGBM":
        import lightgbm as lgb  # noqa: PLC0415

        model = lgb.LGBMClassifier(
            **params, random_state=seed, deterministic=True, force_row_wise=True, verbose=-1
        )
        model.fit(inputs.trees(sample), y, sample_weight=weights)
        info["trees"] = int(model.booster_.num_trees())
    elif family == "XGBoost":
        import xgboost as xgb  # noqa: PLC0415

        model = xgb.XGBClassifier(**params, random_state=seed, enable_categorical=True)
        model.fit(inputs.trees(sample), y, sample_weight=weights)
        info["trees"] = int(model.get_booster().num_boosted_rounds())
    elif family == "CatBoost YetiRank":
        from catboost import CatBoost, Pool  # noqa: PLC0415

        pool = Pool(
            inputs.catboost(sample), label=y, group_id=groups, cat_features=inputs.categorical
        )
        model = CatBoost(
            {**params, "random_seed": seed, "verbose": False, "allow_writing_files": False}
        )
        model.fit(pool)
        info["trees"] = int(model.tree_count_)
    elif family == "MLP":
        from sklearn.neural_network import MLPClassifier  # noqa: PLC0415
        from threadpoolctl import threadpool_limits  # noqa: PLC0415

        spec = {**params, "hidden_layer_sizes": tuple(params["hidden_layer_sizes"])}
        with threadpool_limits(limits=6):
            model = MLPClassifier(**spec, random_state=seed)
            model.fit(inputs.mlp(sample, ranks), y, sample_weight=weights)
        info["epochs"] = int(model.n_iter_)
        info["best_validation_score"] = float(model.best_validation_score_)
        info["inputs"] = int(model.coefs_[0].shape[0])
    else:
        raise ValueError(f"unknown family: {family}")
    return model, info


def score_family(family: str, model, frame: pd.DataFrame, ranks, inputs: Inputs) -> np.ndarray:
    if family in ("LightGBM", "XGBoost"):
        return model.predict_proba(inputs.trees(frame))[:, 1]
    if family == "CatBoost YetiRank":
        return model.predict(inputs.catboost(frame))
    from threadpoolctl import threadpool_limits  # noqa: PLC0415

    with threadpool_limits(limits=6):
        return model.predict_proba(inputs.mlp(frame, ranks))[:, 1]


def log_model(mlflow, family: str, model) -> None:
    if family == "LightGBM":
        mlflow.lightgbm.log_model(model, name="model", pip_requirements=["lightgbm"])
    elif family == "XGBoost":
        mlflow.xgboost.log_model(model, name="model", pip_requirements=["xgboost"])
    elif family == "CatBoost YetiRank":
        mlflow.catboost.log_model(model, name="model", pip_requirements=["catboost"])
    else:
        mlflow.sklearn.log_model(
            model,
            name="model",
            pip_requirements=["scikit-learn"],
            skops_trusted_types=["sklearn.neural_network._stochastic_optimizers.AdamOptimizer"],
        )


def month_frame(con, month: str) -> pd.DataFrame:
    """Candidate channel rows of a month with F2 features (as the v5 scoring query)."""
    lab = _q(LABELS / f"channels/{TARGET}/{month}.parquet")
    f2 = _q(V5["F2"] / f"month={month}/features.parquet")
    return con.execute(f"""
        SELECT l.channel_id, l.issued_at, l.object_id, l.sensor_type, l.candidate,
               f2.* EXCLUDE (channel_id, issued_at, object_id, sensor_type)
        FROM read_parquet({lab}) l JOIN read_parquet({f2}) f2 USING (channel_id, issued_at)
        WHERE l.candidate AND l.issued_at < TIMESTAMP {_q(DEV_END)}
    """).df()


def fit_sample(con, horizon: int, fold: dict) -> pd.DataFrame:
    """v5 fit rows (all positives and the 10% hash sample of negatives, embargo = horizon)
    with F2 features, ordered by (issued_at, channel_id)."""
    ranges = " OR ".join(
        f"(issued_at >= TIMESTAMP {_q(a)} AND issued_at < TIMESTAMP {_q(b)} - "
        f"INTERVAL {horizon} HOUR)"
        for a, b in fold["fit"]
    )
    files = _files(LABELS / f"channels/{TARGET}/{m}.parquet" for m in DEV_MONTHS)
    keys = con.execute(f"""
        SELECT channel_id, issued_at, y_{horizon}h::INT AS y FROM read_parquet({files})
        WHERE split_period = 'development' AND candidate
          AND outcome_{horizon}h IN ('positive', 'negative') AND ({ranges})
    """).df()
    keep = (keys["y"] == 1).to_numpy() | negative_sample_mask(keys.channel_id, keys.issued_at)
    con.register("chosen", keys.loc[keep])
    sample = con.execute(
        f"SELECT chosen.y, * EXCLUDE (y) FROM chosen {V5['_feature_join']([], DEV_MONTHS)} "
        "ORDER BY issued_at, channel_id"
    ).df()
    con.unregister("chosen")
    if len(sample) != int(keep.sum()):
        raise ValueError("features missing for fit rows")
    return sample


def sample_ranks(con, sample: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Within-day ranks of the fit rows among all candidate channels of their day."""
    month = pd.to_datetime(sample["issued_at"]).dt.strftime("%Y-%m")
    parts = []
    for m in sorted(month.unique()):
        full = month_frame(con, m)
        ranks = within_day_ranks(full, columns)
        ranks["channel_id"] = full["channel_id"].to_numpy()
        ranks["issued_at"] = full["issued_at"].to_numpy()
        rows = sample.loc[month == m, ["channel_id", "issued_at"]].reset_index()
        merged = rows.merge(ranks, on=["channel_id", "issued_at"], how="left", validate="1:1")
        parts.append(merged.set_index("index"))
    out = pd.concat(parts).loc[sample.index, columns]
    if len(out) != len(sample):
        raise ValueError("ranks missing for fit rows")
    return out


def score_year(con, year: int, fitted: dict, inputs: Inputs, months=None) -> pd.DataFrame:
    """Channel scores of every fitted model for the candidate rows of the evaluation year."""
    parts = []
    for month in months or year_months(year):
        frame = month_frame(con, month)
        need_ranks = any(f == "MLP" for f, _ in fitted.values())
        ranks = within_day_ranks(frame, inputs.numeric) if need_ranks else None
        ch = frame[["channel_id", "issued_at", "object_id", "sensor_type", "candidate"]].copy()
        for name, (family, model) in fitted.items():
            ch[name] = score_family(family, model, frame, ranks, inputs)
        parts.append(ch)
    return pd.concat(parts, ignore_index=True)


def attach_mean_top2(table: pd.DataFrame, channels: pd.DataFrame, names) -> pd.DataFrame:
    for name in names:
        agg = card_aggregates(channels, name, "mean_top2").rename(columns={"score": name})
        table = table.drop(columns=[name], errors="ignore").merge(agg, on=KEYS, how="left")
    return table


# -- metrics -----------------------------------------------------------------------------


def honest_capture(cards, links, events, mask: np.ndarray, days: int) -> pd.DataFrame:
    """Evaluable events (sorted by id) with the protocol capture, the honest capture (a
    selected card of the pair is open when the event starts: before the day after the
    card's first event and before its window end) and the honest lead from the earliest
    open capturing card."""
    out = ev.event_outcomes(cards, links, events, mask)
    out = out[out["evaluable"]].sort_values("event_id").reset_index(drop=True)
    sel = cards.loc[mask, [*KEYS, "first_event_start", "y_card"]].copy()
    end = pd.to_datetime(sel["issued_at"]) + pd.Timedelta(days=days)
    start = pd.to_datetime(sel["first_event_start"]).where(sel["y_card"] == 1)
    release = (start.dt.floor("D") + DAY).fillna(end)
    sel["release"] = release.where(release <= end, end)
    linked = links.loc[links["candidate_member"].astype(bool), [*KEYS, "event_id"]]
    linked = linked.merge(sel, on=KEYS, how="inner").merge(
        events[["event_id", "event_start"]], on="event_id"
    )
    start_at = pd.to_datetime(linked["event_start"])
    issued = pd.to_datetime(linked["issued_at"])
    linked = linked[(start_at < linked["release"]) & (start_at >= issued)]
    lead = (
        (pd.to_datetime(linked["event_start"]) - pd.to_datetime(linked["issued_at"]))
        .dt.total_seconds()
        .div(3600)
        .groupby(linked["event_id"].to_numpy())
        .max()
        .rename("lead_open")
    )
    out = out.merge(lead, left_on="event_id", right_index=True, how="left")
    out["captured_open"] = out["captured"].astype(bool) & out["lead_open"].notna()
    out.loc[~out["captured_open"], "lead_open"] = np.nan
    return out


def point_metrics(cards, links, events, mask: np.ndarray, days: int, fresh_events=None):
    """Precision, honest and protocol recall, honest lead, new cards per day; with
    ``fresh_events`` the recalls are over fresh events only."""
    evaluable = cards["outcome"].isin(ev.EVALUABLE).to_numpy()
    y = cards["y_card"].to_numpy(dtype=float)
    chosen = mask & evaluable
    h = honest_capture(cards, links, events, mask, days)
    if fresh_events is not None:
        h = h[h["event_id"].isin(fresh_events)]
    lead = h.loc[h["captured_open"], "lead_open"].to_numpy(dtype=float)
    per_day = pd.Series(mask.astype(int)).groupby(cards["issued_at"].to_numpy()).sum()
    return {
        "cards_selected": int(chosen.sum()),
        "card_precision": float(y[chosen].mean()) if chosen.any() else None,
        "event_recall_open": float(h["captured_open"].mean()) if len(h) else None,
        "event_recall_protocol": float(h["captured"].mean()) if len(h) else None,
        "events": len(h),
        "lead_hours_median_open": float(np.median(lead)) if len(lead) else None,
        "lead_share_le_24h": float((lead <= 24).mean()) if len(lead) else None,
        "new_cards_per_day": float(per_day.mean()) if len(per_day) else None,
    }


def oracle_scores(cards, links, events) -> np.ndarray:
    linked = links[links["candidate_member"].astype(bool)]
    linked = linked[linked["event_id"].isin(events.loc[events["qualifies"], "event_id"])]
    counts = linked.groupby(KEYS).event_id.nunique().rename("_events")
    frame = cards[KEYS].merge(counts, on=KEYS, how="left")
    return cards["y_card"].fillna(0).to_numpy(dtype=float) * (
        1 + frame["_events"].fillna(0).to_numpy()
    )


def _interval(point, values: np.ndarray) -> dict:
    out = ev._interval(point, values)
    valid = values[np.isfinite(values)]
    out["share_positive"] = float((valid > 0).mean()) if len(valid) else None
    out["all_replicates_positive"] = bool(len(valid) and (valid > 0).all())
    return out


def block_bootstrap(
    rows: pd.DataFrame,
    sel: dict[str, np.ndarray],
    base: pd.DataFrame,
    captured: dict[str, np.ndarray],
    groups: dict[str, list[str]],
    deltas: list[tuple[str, str]],
    *,
    replicates: int = 400,
    seed: int = 17,
    schemes=("iso_week", "object_id"),
    keep=None,
) -> dict:
    """Paired block bootstrap of card precision and (honest) event recall for fixed issue
    masks, as ``tune_sensor_failure_v5.bootstrap_masks`` (same block keys, seeds and
    draws) but from per-block sums. ``rows`` are the evaluable cards, ``sel`` their
    selection per score, ``base`` the events of the recall and ``captured`` their capture
    per score. A group (seeds of one family) is the mean of its members per replicate;
    deltas ``(a, b)`` are paired a - b; ``keep`` limits the scores printed with intervals."""
    y = rows["y_card"].to_numpy(dtype=float)
    result = {"replicates": replicates, "seed": seed, "schemes": {}}
    for offset, scheme in enumerate(schemes):
        ck = ev._block_keys(rows["issued_at"], rows["object_id"], scheme)
        ek = ev._block_keys(base["event_start"], base["object_id"], scheme)
        codes, uniques = pd.factorize(pd.concat([ck, ek], ignore_index=True))
        cc, ec = codes[: len(rows)], codes[len(rows) :]
        blocks = len(uniques)
        rng = np.random.default_rng(seed + offset)
        draws = rng.integers(0, blocks, size=(replicates, blocks))
        weights = np.vstack(
            [np.ones(blocks), np.stack([np.bincount(d, minlength=blocks) for d in draws])]
        ).astype(float)
        n_events = weights @ np.bincount(ec, minlength=blocks).astype(float)
        values = {}
        for name, s in sel.items():
            with np.errstate(invalid="ignore", divide="ignore"):
                n_sel = weights @ np.bincount(cc, weights=s, minlength=blocks)
                hit = weights @ np.bincount(cc, weights=s * y, minlength=blocks)
                cap = weights @ np.bincount(ec, weights=captured[name], minlength=blocks)
                values[name] = {
                    "card_precision": np.where(n_sel > 0, hit / n_sel, np.nan),
                    "event_recall_open": np.where(n_events > 0, cap / n_events, np.nan),
                }
        for group, members in groups.items():
            values[group] = {
                m: np.mean([values[n][m] for n in members], axis=0) for m in values[members[0]]
            }
        block = {
            "blocks": blocks,
            "scores": {
                n: {m: ev._interval(float(v[0]), v[1:]) for m, v in per.items()}
                for n, per in values.items()
                if keep is None or n in keep
            },
            "deltas": {},
        }
        for a, b in deltas:
            block["deltas"][f"{a} - {b}"] = {
                m: _interval(
                    float(values[a][m][0] - values[b][m][0]), values[a][m][1:] - values[b][m][1:]
                )
                for m in values[a]
            }
        result["schemes"]["object" if scheme == "object_id" else scheme] = block
    return ev._py(result)


def weighted_median(values: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Lower weighted median per row of ``weights`` (values sorted ascending)."""
    cum = np.cumsum(weights, axis=1)
    total = cum[:, -1]
    idx = (cum >= 0.5 * total[:, None]).argmax(axis=1)
    out = values[idx].astype(float)
    out[total <= 0] = np.nan
    return out


def lead_bootstrap(captured: dict[str, pd.DataFrame], pairs, *, replicates=400, seed=17):
    """Median honest lead per score and paired week-block intervals of the difference."""
    ordered = {n: o.sort_values("lead_open").reset_index(drop=True) for n, o in captured.items()}
    stats = {}
    for n, o in ordered.items():
        lead = o["lead_open"].to_numpy(dtype=float)
        stats[n] = {
            "captured": len(lead),
            "median": float(weighted_median(lead, np.ones((1, len(lead))))[0])
            if len(lead)
            else None,
            "share_le_24h": float((lead <= 24).mean()) if len(lead) else None,
            "share_le_72h": float((lead <= 72).mean()) if len(lead) else None,
        }
    keys = {
        n: ev._block_keys(o["event_start"], o["object_id"], "iso_week") for n, o in ordered.items()
    }
    codes, uniques = pd.factorize(pd.concat(list(keys.values()), ignore_index=True))
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(uniques), size=(replicates, len(uniques)))
    weights = np.stack([np.bincount(d, minlength=len(uniques)) for d in draws]).astype(float)
    medians, pos = {}, 0
    for n, o in ordered.items():
        c = codes[pos : pos + len(o)]
        pos += len(o)
        lead = o["lead_open"].to_numpy(dtype=float)
        medians[n] = weighted_median(lead, weights[:, c]) if len(o) else np.full(replicates, np.nan)
    deltas = {}
    for a, b in pairs:
        if stats[a]["median"] is None or stats[b]["median"] is None:
            continue
        deltas[f"{a} - {b}"] = ev._interval(
            float(stats[a]["median"] - stats[b]["median"]), medians[a] - medians[b]
        )
    return ev._py({"stats": stats, "deltas_week": deltas, "blocks": len(uniques)})


def calibration(prob: np.ndarray, y: np.ndarray, edges=PROB_EDGES) -> dict:
    """Score as a probability against the share of cards with an event: fixed bins,
    Wilson intervals (cards of one pair repeat, so the intervals are optimistic), ECE and
    Brier."""
    prob, y = np.asarray(prob, dtype=float), np.asarray(y, dtype=float)
    ok = np.isfinite(prob)
    prob, y = prob[ok], y[ok]
    index = np.clip(np.searchsorted(edges, prob, side="right") - 1, 0, len(edges) - 2)
    bins, gap = [], 0.0
    for b in range(len(edges) - 1):
        part = index == b
        n = int(part.sum())
        if not n:
            continue
        k = int(y[part].sum())
        w = wilson_interval(k, n)
        mean_pred = float(prob[part].mean())
        gap += n * abs(mean_pred - k / n)
        bins.append(
            {
                "bin": [edges[b], edges[b + 1]],
                "n": n,
                "mean_score": mean_pred,
                "share_with_event": k / n,
                "wilson_low": w["low"],
                "wilson_high": w["high"],
                "score_inside_wilson": bool(w["low"] <= mean_pred <= w["high"]),
            }
        )
    return {
        "cards": int(len(y)),
        "bins": bins,
        "ece": gap / len(y) if len(y) else None,
        "brier": float(np.mean((prob - y) ** 2)) if len(y) else None,
        "mean_score": float(prob.mean()) if len(y) else None,
        "share_with_event": float(y.mean()) if len(y) else None,
    }


def fresh_event_ids(events: pd.DataFrame, members: pd.DataFrame, lookback: int) -> set:
    """Events none of whose pairs had an event counted in [start - L, start)."""
    times = pair_event_times(members, RULE, drop_year=None)
    stamps = {
        k: np.sort(g["count_at"].to_numpy(dtype="datetime64[ns]"))
        for k, g in times.groupby(UNIT, sort=False)
    }
    pairs = times[["event_id", *UNIT]].merge(events[["event_id", "event_start"]], on="event_id")
    start = pd.to_datetime(pairs["event_start"]).to_numpy(dtype="datetime64[ns]")
    window = np.timedelta64(int(lookback), "D")
    prior = np.zeros(len(pairs))
    for key, positions in pairs.groupby(UNIT, sort=False).indices.items():
        s, t = stamps[key], start[positions]
        prior[positions] = np.searchsorted(s, t, "left") - np.searchsorted(s, t - window, "left")
    chronic = set(pairs.loc[prior > 0, "event_id"])
    return set(events["event_id"]) - chronic


# -- stage stats -------------------------------------------------------------------------


def stats_stage(config: dict, mlflow_on: bool, out: Path = OUT) -> dict:
    started = perf_counter()
    con = _con(out)
    events, members = V5["events_of"](con, TARGET)
    times = pair_event_times(members, RULE)
    starts = pair_starts(con)
    half_life = float(config["candidates"]["5"]["half_life_days"])
    mlflow = _mlflow(config) if mlflow_on else None
    result = {"folds": {}, "half_life_days": half_life}
    for index, fold in enumerate(config["folds"]):
        t0 = perf_counter()
        boundary = pd.Timestamp(fold["evaluate"][0][0])
        histories = pair_histories(times, starts, boundary)
        bayes = fit_bayes(histories)
        bayes["_half_life"] = half_life
        t1 = perf_counter()
        hawkes = fit_hawkes(histories)
        hawkes_seconds = round(perf_counter() - t1, 1)
        block = {
            "boundary": str(boundary.date()),
            "pairs": len(histories),
            "events": int(sum(h["n"] for h in histories)),
            "bayes": bayes,
            "hawkes": hawkes,
            "hawkes_seconds": hawkes_seconds,
            "metrics": {},
        }
        for horizon, days in HORIZONS.items():
            cards, links = load_cards(horizon, index)
            table = cards[KEYS].copy()
            table[BAYES] = bayes_scores(cards, times, starts, bayes, days)
            table[HAWKES] = hawkes_scores(cards, times, hawkes, days)
            folder = out / f"{horizon}h"
            folder.mkdir(parents=True, exist_ok=True)
            table.to_parquet(folder / f"stats_fold{index}.parquet", index=False)
            scored = cards.assign(
                **{BAYES: table[BAYES].to_numpy(), HAWKES: table[HAWKES].to_numpy()}
            )
            block["metrics"][f"{horizon}h"] = {
                name: {
                    str(k): point_metrics(
                        scored, links, events, select(scored, name, days, k), days
                    )
                    for k in config["horizons"][f"{horizon}h"]["budgets"]
                }
                for name in (BAYES, HAWKES)
            }
        block["seconds"] = round(perf_counter() - t0, 1)
        result["folds"][str(index)] = block
        print(
            f"stats fold{index}: {block['seconds']} s (Hawkes MLE {hawkes_seconds} s)", flush=True
        )
        if mlflow is not None:
            for name, params, note in (
                (
                    BAYES,
                    bayes,
                    "Кандидат 5: пуассон-гамма пары с затуханием 90 сут, приор по типу "
                    "(метод моментов); без обучения градиентом.",
                ),
                (
                    HAWKES,
                    hawkes,
                    "Кандидат 7: интенсивность пары mu + sum alpha exp(-dt/tau), "
                    "параметры по типу — максимум правдоподобия на годах fit; без CatBoost.",
                ),
            ):
                with mlflow.start_run(run_name=f"stat {name} ge2s fold{index}"):
                    mlflow.set_tags(
                        {
                            "kind": "candidate_stat",
                            "is_model": "false",
                            "family": name,
                            "fold": str(index),
                            "tuning_version": config["version"],
                            "mlflow.note.content": note,
                        }
                    )
                    mlflow.log_dict(ev._py(params), "params.json")
                    for h, per in block["metrics"].items():
                        mlflow.log_metrics(
                            {
                                f"{h}_K{k}_{m}": float(v[m])
                                for k, v in per[name].items()
                                for m in ("card_precision", "event_recall_open")
                                if v[m] is not None
                            }
                        )
    result["seconds"] = round(perf_counter() - started, 1)
    (out / "stats.json").write_text(
        json.dumps(ev._py(result), ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return result


# -- stage fit ---------------------------------------------------------------------------


def aborted_families(p_fold0: dict[str, dict[int, float]], persistence: dict[int, float]):
    """Families whose seed-17 precision on fold 2023 is below persistence at every K."""
    return sorted(
        f
        for f, by_k in p_fold0.items()
        if all(by_k[k] is not None and by_k[k] < persistence[k] for k in persistence)
    )


def _mlflow(config: dict):
    import mlflow  # noqa: PLC0415

    mlflow.set_tracking_uri(config["mlflow"]["tracking_uri"])
    mlflow.set_experiment(config["mlflow"]["experiment"])
    return mlflow


def fit_horizon(config: dict, horizon: int, mlflow_on: bool, smoke: bool = False) -> dict:
    started = perf_counter()
    out = OUT / "smoke" if smoke else OUT
    days = HORIZONS[horizon]
    budgets = config["horizons"][f"{horizon}h"]["budgets"]
    seeds = config["seeds"]
    con = _con(out)
    events, _ = V5["events_of"](con, TARGET)
    mlflow = _mlflow(config) if mlflow_on and not smoke else None
    folder = out / f"{horizon}h"
    folder.mkdir(parents=True, exist_ok=True)
    params = {f: family_params(config, f) for f in FAMILIES}
    if smoke:
        params["LightGBM"]["n_estimators"] = 5
        params["XGBoost"]["n_estimators"] = 5
        params["CatBoost YetiRank"]["iterations"] = 5
        params["MLP"]["max_iter"] = 2
    aborted: list[str] = []
    result = {"horizon": horizon, "fits": [], "abort": None, "folds": {}}
    for index, fold in enumerate(config["folds"]):
        t0 = perf_counter()
        sample = fit_sample(con, horizon, fold)
        if smoke:
            sample = sample.iloc[:: max(1, len(sample) // 30000)].reset_index(drop=True)
        columns = V5["set_columns"](sample, config["features"]["prefixes"])
        numeric, _ = split_kinds(columns, sample)
        ranks = sample_ranks(con, sample, numeric)
        inputs = Inputs(sample, columns, ranks)
        y = sample["y"].to_numpy(dtype=int)
        groups = ((pd.to_datetime(sample["issued_at"]) - ORIGIN) // DAY).to_numpy()
        sample_seconds = round(perf_counter() - t0, 1)
        print(f"{horizon}h fold{index}: sample {len(sample)} rows ({sample_seconds} s)", flush=True)
        cards, links = load_cards(horizon, index)
        table = cards[KEYS].copy()
        year = fold_year(fold)
        months = year_months(year)[:1] if smoke else None
        if index == 0:
            rounds = [
                [(f, seeds[0]) for f in FAMILIES],
                [(f, s) for f in FAMILIES for s in seeds[1:]],
            ]
        else:
            rounds = [
                [(f, s) for f in FAMILIES for s in seeds if s == seeds[0] or f not in aborted]
            ]
        for r, plan in enumerate(rounds):
            if index == 0 and r == 1:
                plan = [(f, s) for f, s in plan if f not in aborted]
            if not plan:
                continue
            fitted, runs = {}, {}
            for family, seed in plan:
                name = f"{family} s{seed}"
                t1 = perf_counter()
                model, extra = fit_family(
                    family, params[family], sample, y, groups, ranks, inputs, seed
                )
                info = {
                    "horizon": horizon,
                    "fold": index,
                    "family": family,
                    "seed": seed,
                    "threads": 6,
                    "fit_rows": len(sample),
                    "fit_positive": int(y.sum()),
                    "columns": len(columns),
                    "seconds": round(perf_counter() - t1, 1),
                    **extra,
                }
                result["fits"].append(info)
                fitted[name] = (family, model)
                print(f"{horizon}h fold{index} {name}: {info['seconds']} s {extra}", flush=True)
                if mlflow is not None:
                    run_name = config["mlflow"]["fit_run_name"].format(
                        family=family, horizon=f"{horizon}h", fold=index, seed=seed
                    )
                    with mlflow.start_run(run_name=run_name) as run:
                        mlflow.set_tags(
                            {
                                "kind": "fit",
                                "is_model": "true",
                                "family": family,
                                "seed": str(seed),
                                "fold": str(index),
                                "tuning_version": config["version"],
                                "target": TARGET,
                                "horizon": f"{horizon}h",
                            }
                        )
                        mlflow.log_params(
                            {
                                **{k: v for k, v in info.items() if k != "seconds"},
                                **{f"param_{k}": v for k, v in params[family].items()},
                            }
                        )
                        mlflow.log_metric("fit_seconds", info["seconds"])
                        mlflow.log_dict(
                            {
                                "columns": columns,
                                "categories": inputs.categories,
                                "missing_indicators": inputs.missing,
                            },
                            "inputs.json",
                        )
                        log_model(mlflow, family, model)
                        runs[name] = run.info.run_id
            t2 = perf_counter()
            channels = score_year(con, year, fitted, inputs, months)
            table = attach_mean_top2(table, channels, fitted)
            del channels
            scored = cards.merge(table, on=KEYS, how="left")
            metrics = {
                name: {
                    k: point_metrics(scored, links, events, select(scored, name, days, k), days)
                    for k in budgets
                }
                for name in fitted
            }
            print(
                f"{horizon}h fold{index} round{r}: scored in {round(perf_counter() - t2)} s",
                flush=True,
            )
            if mlflow is not None:
                for name, run_id in runs.items():
                    with mlflow.start_run(run_id=run_id):
                        mlflow.log_metrics(
                            {
                                f"K{k}_{m}": float(v[m])
                                for k, v in metrics[name].items()
                                for m in ("card_precision", "event_recall_open")
                                if v[m] is not None
                            }
                        )
            if index == 0 and r == 0:
                persistence = {
                    k: point_metrics(scored, links, events, select(scored, PERSIST, days, k), days)[
                        "card_precision"
                    ]
                    for k in budgets
                }
                p_fold0 = {
                    f: {k: metrics[f"{f} s{seeds[0]}"][k]["card_precision"] for k in budgets}
                    for f in FAMILIES
                }
                aborted = aborted_families(p_fold0, persistence)
                result["abort"] = {
                    "rule": config["abort_rule"],
                    "persistence_fold0": persistence,
                    "seed17_fold0": p_fold0,
                    "aborted": aborted,
                }
                print(f"{horizon}h abort rule: {aborted or 'none'}", flush=True)
        table.to_parquet(folder / f"ml_fold{index}.parquet", index=False)
        result["folds"][str(index)] = {
            "seconds": round(perf_counter() - t0, 1),
            "sample_seconds": sample_seconds,
        }
        del sample, ranks
    result["seconds"] = round(perf_counter() - started, 1)
    result["hygiene"] = {
        "threads": 6,
        "os_cpu_count": os.cpu_count(),
        "cpu_model": subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True
        ).stdout.strip(),
        "versions": _versions(),
    }
    (folder / "fits.json").write_text(
        json.dumps(ev._py(result), ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return result


# -- stage report ------------------------------------------------------------------------


def assemble(horizon: int) -> tuple[list, dict]:
    """Folds (cards with every score, links) and the score columns per family."""
    folds = []
    for i in range(2):
        cards, links = load_cards(horizon, i)
        for part in ("stats", "ml"):
            path = OUT / f"{horizon}h" / f"{part}_fold{i}.parquet"
            if path.exists():
                cards = cards.merge(pd.read_parquet(path), on=KEYS, how="left")
        folds.append((cards, links))
    present = [c for c in folds[0][0].columns if c in folds[1][0].columns]
    families = {f: sorted(c for c in present if c.startswith(f"{f} s")) for f in FAMILIES}
    families = {f: cols for f, cols in families.items() if cols}
    for name in (BAYES, HAWKES):
        if name in present:
            families[name] = [name]
    families[F2V5] = sorted(c for c in present if c.startswith(F2V5))
    return folds, families


def pooled(folds):
    cards = pd.concat([c for c, _ in folds], ignore_index=True)
    links = pd.concat([lk for _, lk in folds], ignore_index=True)
    return cards, links


def evaluate_scores(folds, events, scores: list[str], days: int, budgets, fresh=None) -> dict:
    """Masks per fold and point metrics per fold and pooled; ``fresh`` = (card masks per
    fold, fresh event ids) restricts the list to fresh cards and the recall to fresh events."""
    cards_all, links_all = pooled(folds)
    out = {}
    for k in budgets:
        out[k] = {}
        for score in scores:
            masks, per_fold = [], []
            for i, (cards, links) in enumerate(folds):
                eligible = None if fresh is None else fresh[0][i]
                mask = select(cards, score, days, k, eligible)
                masks.append(mask)
                per_fold.append(
                    point_metrics(
                        cards, links, events, mask, days, None if fresh is None else fresh[1]
                    )
                )
            mask = np.concatenate(masks)
            capture = honest_capture(cards_all, links_all, events, mask, days)
            out[k][score] = {
                "mask": mask,
                "capture": capture,
                "folds": per_fold,
                "mean": {
                    m: float(np.mean([f[m] for f in per_fold]))
                    for m in per_fold[0]
                    if all(f[m] is not None for f in per_fold)
                },
            }
    return out


def bootstrap_block(
    folds, evaluated: dict, groups: dict, deltas, replicates, fresh_ids=None, keep=None
):
    cards_all, _ = pooled(folds)
    evaluable = cards_all["outcome"].isin(ev.EVALUABLE).to_numpy()
    rows = cards_all[evaluable].reset_index(drop=True)
    names = sorted({n for g in groups.values() for n in g})
    base = evaluated[names[0]]["capture"]
    if fresh_ids is not None:
        base = base[base["event_id"].isin(fresh_ids)]
    base = base.reset_index(drop=True)
    sel = {n: evaluated[n]["mask"][evaluable].astype(float) for n in names}
    captured = {}
    for n in names:
        cap = evaluated[n]["capture"].set_index("event_id")["captured_open"]
        captured[n] = cap.reindex(base["event_id"]).fillna(False).to_numpy(dtype=float)
    return block_bootstrap(
        rows, sel, base, captured, groups, deltas, replicates=replicates, seed=17, keep=keep
    )


def seed_summary(evaluated: dict, members: list[str]) -> dict:
    out = {}
    for m in (
        "card_precision",
        "event_recall_open",
        "event_recall_protocol",
        "lead_hours_median_open",
        "new_cards_per_day",
    ):
        vals = [evaluated[n]["mean"].get(m) for n in members]
        if any(v is None for v in vals):
            continue
        folds = [[evaluated[n]["folds"][i][m] for n in members] for i in range(2)]
        out[m] = {
            "mean": float(np.mean(vals)),
            "min": float(np.min(vals)),
            "max": float(np.max(vals)),
            "spread": float(np.max(vals) - np.min(vals)),
            "folds_mean": [float(np.mean(v)) for v in folds],
        }
    return out


def best_ml(boot: dict, families: dict, budgets) -> str | None:
    """Family among candidates 1-4 with the highest mean over its seeds and the horizon's
    K of the pooled precision (ties in the order of FAMILIES)."""
    rows = []
    for order, f in enumerate(FAMILIES):
        if f not in families:
            continue
        p = [
            boot[k]["schemes"]["iso_week"]["scores"][f]["card_precision"]["point"] for k in budgets
        ]
        rows.append((-float(np.mean(p)), order, f))
    return min(rows)[2] if rows else None


def criterion(boot: dict, evaluated: dict, family: str, members: list[str]) -> dict:
    week, obj = boot["schemes"]["iso_week"], boot["schemes"]["object"]
    delta = week["deltas"][f"{family} - {LIST}"]["card_precision"]
    p = week["scores"][family]["card_precision"]["point"]
    p_list = week["scores"][LIST]["card_precision"]["point"]
    r = week["scores"][family]["event_recall_open"]["point"]
    r_list = week["scores"][LIST]["event_recall_open"]["point"]
    fold_delta = [
        float(np.mean([evaluated[n]["folds"][i]["card_precision"] for n in members]))
        - evaluated[LIST]["folds"][i]["card_precision"]
        for i in range(2)
    ]
    checks = {
        "1_mean_precision_above_list": p > p_list,
        "2_week_ci_above_0": delta["low"] is not None and delta["low"] > 0,
        "3_each_fold_above_list": all(d > 0 for d in fold_delta),
        "4_honest_recall_not_below_list_minus_0.02": r >= r_list - 0.02,
    }
    return {
        "seeds": len(members),
        "precision": p,
        "precision_list": p_list,
        "precision_minus_list_week": delta,
        "precision_minus_list_object": obj["deltas"][f"{family} - {LIST}"]["card_precision"],
        "precision_minus_list_per_fold": fold_delta,
        "recall_open": r,
        "recall_open_list": r_list,
        "recall_open_minus_list_week": week["deltas"][f"{family} - {LIST}"]["event_recall_open"],
        "checks": checks,
        "better_than_statistics": all(checks.values()),
        "bonferroni_all_replicates_positive": bool(delta.get("all_replicates_positive")),
    }


def clean(evaluated: dict) -> dict:
    return {
        k: {n: {"folds": v["folds"], "mean": v["mean"]} for n, v in per.items()}
        for k, per in evaluated.items()
    }


def horizon_report(horizon: int, config: dict, events, replicates: int) -> tuple[dict, list, dict]:
    days = HORIZONS[horizon]
    budgets = config["horizons"][f"{horizon}h"]["budgets"]
    folds, families = assemble(horizon)
    folds = [(c.assign(**{CEIL: oracle_scores(c, lk, events)}), lk) for c, lk in folds]
    ml = {f: families[f] for f in FAMILIES if f in families}
    names = [LIST, PERSIST, *families[F2V5], *(n for f in ml for n in ml[f])]
    names += [n for n in (BAYES, HAWKES) if n in families]
    evaluated = evaluate_scores(folds, events, [*names, CEIL], days, budgets)
    groups = {f: cols for f, cols in families.items()}
    groups.update({LIST: [LIST], PERSIST: [PERSIST]})
    first = {
        k: bootstrap_block(folds, evaluated[k], {f: groups[f] for f in ml}, [], replicates)
        for k in budgets
    }
    best = best_ml(first, ml, budgets)
    stack_names = []
    if best is not None:
        stack = f"Stack {best}+list"
        for i, (cards, links) in enumerate(folds):
            listed = day_rank(cards, LIST)
            for col in families[best]:
                cards[f"Stack {col}+list"] = 0.5 * (day_rank(cards, col) + listed)
            folds[i] = (cards, links)
        stack_names = [f"Stack {col}+list" for col in families[best]]
        extra = evaluate_scores(folds, events, stack_names, days, budgets)
        for k in budgets:
            evaluated[k].update(extra[k])
        groups[stack] = stack_names
    order = (*FAMILIES, BAYES, f"Stack {best}+list" if best else None, HAWKES)
    candidates = [c for c in order if c is not None and c in groups]
    deltas = [(c, LIST) for c in candidates]
    deltas += [(c, PERSIST) for c in candidates] + [(c, F2V5) for c in candidates]
    deltas += [(F2V5, LIST), (PERSIST, LIST)]
    deltas += [(n, LIST) for c in [*candidates, F2V5] if len(groups[c]) > 1 for n in groups[c]]
    boot, crit, seeds, lead = {}, {}, {}, {}
    for k in budgets:
        boot[k] = bootstrap_block(folds, evaluated[k], groups, deltas, replicates)
        crit[k] = {c: criterion(boot[k], evaluated[k], c, groups[c]) for c in candidates}
        seeds[k] = {g: seed_summary(evaluated[k], m) for g, m in groups.items()}
        caps = {
            n: evaluated[k][n]["capture"].loc[
                lambda o: o["captured_open"], ["event_id", "event_start", "object_id", "lead_open"]
            ]
            for n in [*names, *stack_names]
        }
        lead[k] = lead_bootstrap(
            caps, [(n, LIST) for n in [*names, *stack_names] if n != LIST], replicates=replicates
        )
    block = {
        "days": days,
        "budgets": budgets,
        "families": families,
        "best_ml_for_stacking": best,
        "stacking": stack_names,
        "candidates": candidates,
        "points": clean(evaluated),
        "seed_summary": seeds,
        "bootstrap": boot,
        "criterion": crit,
        "lead": lead,
    }
    return block, folds, groups


def fresh_report(folds, groups, events, members, replicates) -> dict:
    """Fresh-only lists at 7 days, K = 2 and 3: every candidate, its rank mean with the list
    and the references against the list in the same mode."""
    days = HORIZONS[FRESH_HORIZON]
    rank_of = {}
    for i, (cards, links) in enumerate(folds):
        listed = day_rank(cards, LIST)
        for f in (*FAMILIES, BAYES, HAWKES, F2V5):
            for col in groups.get(f, []):
                cards[f"rank {col}+list"] = 0.5 * (day_rank(cards, col) + listed)
                rank_of.setdefault(f"rank {f}+list", []).append(f"rank {col}+list")
        folds[i] = (cards, links)
    rank_groups = {g: sorted(set(cols)) for g, cols in rank_of.items()}
    all_groups = {**groups, **rank_groups}
    names = sorted({n for cols in all_groups.values() for n in cols})
    out = {"days": days, "budgets": list(FRESH_BUDGETS), "lookbacks": {}}
    compared = [g for g in all_groups if g != LIST]
    for lookback in (14, 7):
        fresh_ids = fresh_event_ids(events, members, lookback)
        fresh_cards = [
            ev.static_list_scores(c, members, RULE, window_days=lookback).to_numpy() == 0
            for c, _ in folds
        ]
        evaluated = evaluate_scores(
            folds, events, names, days, FRESH_BUDGETS, fresh=(fresh_cards, fresh_ids)
        )
        block = {
            "fresh_cards_share": [
                float(f[c["outcome"].isin(ev.EVALUABLE).to_numpy()].mean())
                for f, (c, _) in zip(fresh_cards, folds, strict=True)
            ],
            "fresh_events_share": None,
            "points": {},
            "seed_summary": {},
            "bootstrap": {},
        }
        base = evaluated[FRESH_BUDGETS[0]][LIST]["capture"]
        block["fresh_events_share"] = float(base["event_id"].isin(fresh_ids).mean())
        for k in FRESH_BUDGETS:
            block["points"][k] = clean({k: evaluated[k]})[k]
            block["seed_summary"][k] = {
                g: seed_summary(evaluated[k], m) for g, m in all_groups.items()
            }
            block["bootstrap"][k] = bootstrap_block(
                folds,
                evaluated[k],
                all_groups,
                [(g, LIST) for g in compared],
                replicates,
                fresh_ids,
                keep=set(all_groups),
            )
        out["lookbacks"][str(lookback)] = block
    out["comparisons"] = len(compared) * len(FRESH_BUDGETS) * 2
    return out


def calibration_report(folds, days: int, budgets, evaluated_points=None) -> dict:
    """Candidates 5 and 7 as probabilities: all evaluable cards at cutoffs D days apart and
    the cards issued at each K, per year."""
    out = {}
    for name in (HAWKES, BAYES):
        if name not in folds[0][0]:
            continue
        per = {"spaced_all_pairs": [], "issued": {str(k): [] for k in budgets}}
        for cards, _ in folds:
            evaluable = cards["outcome"].isin(ev.EVALUABLE).to_numpy()
            day = pd.to_datetime(cards["issued_at"])
            spaced = ((day - day.min()).dt.days % days == 0).to_numpy() & evaluable
            y = cards["y_card"].to_numpy(dtype=float)
            per["spaced_all_pairs"].append(calibration(cards[name].to_numpy()[spaced], y[spaced]))
            for k in budgets:
                mask = select(cards, name, days, k) & evaluable
                per["issued"][str(k)].append(calibration(cards[name].to_numpy()[mask], y[mask]))
        out[name] = per
    return out


def reference_checks(block7: dict, block14: dict) -> dict:
    """The list's honest recall per fold against explorations 3 and 4 (same definition)."""
    out = {}
    if EXPLORATION3.exists():
        e3 = json.loads(EXPLORATION3.read_text(encoding="utf-8"))
        for k in (3, 7):
            theirs = [
                f["event_recall_open"] for f in e3["seven_days"]["curve"][LIST][str(k)]["folds"]
            ]
            ours = [f["event_recall_open"] for f in block7["points"][k][LIST]["folds"]]
            out[f"168h K{k} list honest recall vs exploration 3"] = float(
                np.max(np.abs(np.subtract(theirs, ours)))
            )
    if EXPLORATION4.exists():
        e4 = json.loads(EXPLORATION4.read_text(encoding="utf-8"))
        grid = e4["grid"]["ge2s 14d"]["policies"]["release"][LIST]
        for k in (10, 12, 14):
            theirs = [f["event_recall_open"] for f in grid[str(k)]["folds"]]
            ours = [f["event_recall_open"] for f in block14["points"][k][LIST]["folds"]]
            out[f"336h K{k} list honest recall vs exploration 4"] = float(
                np.max(np.abs(np.subtract(theirs, ours)))
            )
    return out


def report(config: dict, replicates: int) -> dict:
    started = perf_counter()
    con = _con()
    events, members = V5["events_of"](con, TARGET)
    horizons, kept = {}, {}
    for horizon in HORIZONS:
        t0 = perf_counter()
        block, folds, groups = horizon_report(horizon, config, events, replicates)
        days = HORIZONS[horizon]
        block["calibration"] = calibration_report(folds, days, block["budgets"])
        block["seconds"] = round(perf_counter() - t0, 1)
        horizons[f"{horizon}h"] = block
        kept[horizon] = (folds, groups)
        print(f"report {horizon}h done ({block['seconds']} s)", flush=True)
    t0 = perf_counter()
    folds, groups = kept[FRESH_HORIZON]
    fresh = fresh_report(folds, groups, events, members, replicates)
    fresh["seconds"] = round(perf_counter() - t0, 1)
    print(f"fresh slice done ({fresh['seconds']} s)", flush=True)
    fits = {}
    for horizon in HORIZONS:
        path = OUT / f"{horizon}h" / "fits.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            fits[f"{horizon}h"] = {
                "fits": data["fits"],
                "abort": data["abort"],
                "folds": data["folds"],
                "seconds": data["seconds"],
                "hygiene": data["hygiene"],
            }
    stats_path = OUT / "stats.json"
    stats = json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.exists() else None
    n_candidates = len(horizons["168h"]["candidates"])
    comparisons = sum(len(b["candidates"]) * len(b["budgets"]) for b in horizons.values())
    return {
        "stage": "v6 report (target >= 2 s, folds 2023/2024; calibration and holdout not read)",
        "status": config["status"],
        "config": str(CONFIG.relative_to(ROOT)),
        "note": "selection on the development folds; a candidate that meets the criterion needs "
        "a separate final check with its own pre-registration",
        "policy": config["policy"],
        "recall": config["recall"],
        "criterion": config["criterion"],
        "multiplicity": {
            "candidates": n_candidates,
            "criterion_comparisons_with_list": comparisons,
            "bonferroni_alpha_per_comparison": 0.05 / comparisons,
            "bonferroni_reading": "with 400 replicates a one-sided 0.05/n test needs every "
            "replicate of the week bootstrap above 0 (field bonferroni_all_replicates_positive)",
            "stacking": "its ML member is chosen on the same folds",
            "fresh_slice_comparisons": fresh["comparisons"],
        },
        "stats": stats,
        "fits": fits,
        "horizons": horizons,
        "fresh_7d": fresh,
        "checks": reference_checks(horizons["168h"], horizons["336h"]),
        "seconds": round(perf_counter() - started, 1),
    }


def log_report_runs(config: dict, result: dict) -> None:
    mlflow = _mlflow(config)
    for horizon, block in result["horizons"].items():
        for key in [LIST, PERSIST, F2V5, CEIL, *block["candidates"]]:
            if key in REFERENCE_RUNS:
                name, note = REFERENCE_RUNS[key]
                tags = {"kind": "reference", "reference": key, "is_model": "false"}
            else:
                name, note = (
                    f"{key} (сводка по seed)",
                    "Сводка кандидата v6 по seed: среднее P и честного R.",
                )
                tags = {"kind": "candidate_summary", "family": key, "is_model": "false"}
            with mlflow.start_run(run_name=f"{name} ge2s {horizon}"):
                mlflow.set_tags(
                    {
                        **tags,
                        "tuning_version": config["version"],
                        "horizon": horizon,
                        "mlflow.note.content": note,
                    }
                )
                metrics = {}
                for k in block["budgets"]:
                    if key == CEIL:
                        vals = block["points"][k][CEIL]["mean"]
                        summary = {m: {"mean": v} for m, v in vals.items()}
                    else:
                        summary = block["seed_summary"][k].get(key, {})
                    for m in ("card_precision", "event_recall_open", "lead_hours_median_open"):
                        if m in summary:
                            metrics[f"K{k}_{m}"] = float(summary[m]["mean"])
                mlflow.log_metrics(metrics)


def _versions() -> dict:
    out = {}
    for package in ("lightgbm", "xgboost", "catboost", "scikit-learn", "numpy", "pandas", "scipy"):
        try:
            out[package] = version(package)
        except Exception:  # noqa: BLE001
            out[package] = None
    return out


def _provenance() -> dict:
    return {
        "config_sha256": _sha(CONFIG),
        "labels_summary_sha256": _sha(LABELS / "summary.json"),
        "versions": _versions(),
        "git_sha": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
        ).stdout.strip(),
        "git_dirty": bool(
            subprocess.run(
                ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True
            ).stdout.strip()
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["stats", "fit", "report"], required=True)
    parser.add_argument("--horizon", type=int, choices=sorted(HORIZONS))
    parser.add_argument("--replicates", type=int, default=400)
    parser.add_argument("--no-mlflow", action="store_true")
    parser.add_argument("--smoke", action="store_true", help="tiny fits, one month, no MLflow")
    args = parser.parse_args()
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    if not config["status"].startswith("frozen_before_fit"):
        raise SystemExit("v6 plan is not frozen")
    if not args.no_mlflow and not args.smoke:
        from mlflow.tracking import MlflowClient  # noqa: PLC0415

        client = MlflowClient(config["mlflow"]["tracking_uri"])
        if client.get_experiment_by_name(config["mlflow"]["experiment"]) is None:
            client.create_experiment(config["mlflow"]["experiment"])
    if args.stage == "stats":
        stats_stage(config, not args.no_mlflow)
    elif args.stage == "fit":
        if args.horizon is None:
            raise SystemExit("--horizon is required for --stage fit")
        fit_horizon(config, args.horizon, not args.no_mlflow, smoke=args.smoke)
    else:
        result = {**report(config, args.replicates), **_provenance()}
        text = json.dumps(ev._py(result), ensure_ascii=False, indent=1) + "\n"
        if any(k in text for k in ('"object_id"', '"channel_id"', '"event_id"', '"node_id"')):
            raise ValueError("report must not contain identifiers")
        REPORT.write_text(text, encoding="utf-8")
        if not args.no_mlflow:
            log_report_runs(config, result)
        print(f"report: {REPORT} ({result['seconds']} s)")


if __name__ == "__main__":
    main()

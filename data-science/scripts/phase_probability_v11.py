"""Phase: a probability layer on top of the static list (v11; data analyst task 27.09.2026).

The ranking of the product stays the static list (v9 prod_phase: 14 days, K = 10, release,
causal eligibility). The question is whether each issued card can carry a model probability
of at least one event in its 14-day window instead of the «k из n» share.

Pre-registered protocol (fixed in this header before any computation; the technologist's
corrections of 27.09.2026 arrived before the first run and are included)
-----------------------------------------------------------------------------------------
Model M (main), empirical Bayes Poisson–gamma on INCIDENTS, no training, past only. At a
cutoff t, for every phase pair i (object × «Состояние фазы») of the cutoff:
  n_i = incident starts of the pair in [t − 365 d, t) (events of a pair < 24 h apart are one
        incident, chained over all events; series of events would inflate a Poisson rate);
  E_i = observed days of the pair in that window (365, or less for a younger pair);
  prior by the method of moments over the pairs of the cutoff:
    m = Σ n_i / Σ E_i;  v = var(n_i / E_i, ddof = 1) − m · mean(1 / E_i);
    α = m² / v, β = m / v  (if v ≤ 0 or m = 0: α = 1, β = 1 / max(m, 1e−9));
  P_i(≥ 1 event in D = 14 d) = 1 − E[exp(−λ D)] = 1 − ((β + E_i) / (β + E_i + D))^(α + n_i).
  An incident starts with an event, so «≥ 1 incident in the window» = «≥ 1 event» (y_card).
References (reported, no criterion): M-decay90 — incidents and exposure weighted by
2^(−age / 90 d) (history from 2022-01-01); M-events — n_i counts events (the static-list
count); naive — 1 − exp(−D · n_i / E_i) on incidents; «k из n» — the product table: three bins
of the 365-day event count [0, 37], [38, 62], [63, ∞) with the share of issued cards that had an
event, built on the OTHER dev year (on dev 2023 + 2024 for the holdout, as the product).
Displayed value (technologist): the card shows M rounded to 5 percentage points, values above
0.90 as «> 90%». Calibration is also evaluated on the displayed values («> 90%» enters Brier and
ECE with the mean of its raw forecasts; it is «inside» if the Wilson upper bound is > 0.90).
Every displayed value carries «из карточек с такой оценкой сбылось k из n» (dev 2023 + 2024
issued cards); a displayed value whose k/n Wilson interval on dev does not contain it is not
shown in the card — the report lists which values pass.
Evaluation set (primary): issued cards with a known outcome of prod_phase in the evaluation
year; secondary: all known eligible phase pair-days. Directions: 2023 → 2024 and 2024 → 2023
(«→» names the year of the «k из n» table; M has no fitted parameters).
Metrics: Brier, log loss, ECE and a reliability table on 5 equal-count bins of the forecast
(bins with tied forecasts merged, so «k из n» has at most 3), per bin k, n, the observed share
with its Wilson 95% interval and the mean forecast; a bin is «inside» if its mean forecast lies
in the Wilson interval.
Criterion (M, primary set), for the raw AND for the displayed values: in BOTH directions
ECE(M) ≤ ECE(«k из n») and at least 4 of 5 bins inside. Only then may the card say
«вероятность» with the note «модель интенсивности, откалибрована на 2023–2024». Ranking
agreement of M with the list (same issued cards at K = 10 if the list is replaced by M) is
reported.
Holdout 2025-07..2026-06: SIXTH use, calibration only, descriptive. M has no parameters to
choose and the decision above rests on dev only; the holdout numbers are reported beside it.

    python data-science/scripts/phase_probability_v11.py
"""

from __future__ import annotations

import json
import runpy
from math import log
from pathlib import Path
from time import perf_counter

import numpy as np
import pandas as pd

from infra_pulse_research.modeling import sensor_failure_release as rel
from infra_pulse_research.modeling.target_audit import wilson_interval

HERE = Path(__file__).resolve().parent
PH = runpy.run_path(str(HERE / "posthoc_sensor_failure_v9_baselines.py"), run_name="ph")
V9 = PH["V9"]
ROOT = V9["ROOT"]
OUT = ROOT / "data-science/artifacts/next-2026-09-27/v11"
REPORT = ROOT / "data-science/reports/sensor-failure-phase-probability-v11-2026-09-27.json"
KEYS, UNIT = rel.CARD_KEYS, rel.UNIT
D = 14
K = 10
WINDOW = 365
HALF_LIFE = 90.0
HISTORY_FROM = pd.Timestamp("2022-01-01")
N_BINS = 5
KOFN_EDGES = ((0, 37), (38, 62), (63, None))
DIRECTIONS = (("2023", "2024"), ("2024", "2023"))
METHODS = ("bayes", "bayes_display", "bayes_decay90", "bayes_events", "naive", "k_of_n")
STEP = 0.05
CAP = 0.90
CRITERION = {"ece_not_worse_than": "k_of_n", "inside_bins_min": 4, "bins": N_BINS}
DAY = np.timedelta64(1, "D")


# -- the model (the runtime formula) --------------------------------------------------------


def gamma_prior(n: np.ndarray, e: np.ndarray) -> tuple[float, float]:
    """Method-of-moments gamma prior of Poisson rates with exposures ``e`` (days)."""
    n = np.asarray(n, dtype=float)
    e = np.asarray(e, dtype=float)
    keep = e > 0
    n, e = n[keep], e[keep]
    m = n.sum() / e.sum() if e.sum() > 0 else 0.0
    v = float(np.var(n / e, ddof=1) - m * np.mean(1.0 / e)) if len(n) > 1 else 0.0
    if m <= 0 or not v > 0:
        return 1.0, 1.0 / max(m, 1e-9)
    return m * m / v, m / v


def p_at_least_one(n, e, alpha: float, beta: float, days: float = D) -> np.ndarray:
    """1 − E[exp(−λ·days)] for λ ~ Gamma(alpha + n, beta + e) (rate parametrisation)."""
    a = alpha + np.asarray(n, dtype=float)
    b = beta + np.asarray(e, dtype=float)
    return -np.expm1(a * np.log(b / (b + days)))


def bayes_by_cutoff(frame: pd.DataFrame, n_col: str, e_col: str) -> np.ndarray:
    """M at every cutoff: the prior over the pairs of that cutoff, then the posterior."""
    out = np.full(len(frame), np.nan)
    for _, pos in frame.groupby("issued_at", sort=False).indices.items():
        n = frame[n_col].to_numpy(dtype=float)[pos]
        e = frame[e_col].to_numpy(dtype=float)[pos]
        alpha, beta = gamma_prior(n, e)
        out[pos] = p_at_least_one(n, e, alpha, beta)
    return out


# -- inputs --------------------------------------------------------------------------------


def pair_first_seen(con, root: Path, end: str) -> pd.Series:
    """First 7-day card of every phase pair before ``end`` (the pair is observed from it)."""
    months = [
        p.strftime("%Y-%m")
        for p in pd.period_range("2019-01-01", pd.Timestamp(end) - pd.Timedelta(days=1), freq="M")
        if p.year != 2021
    ]
    files = [root / f"cards/{V9['TARGET']}/168h/{m}.parquet" for m in months]
    files = [f for f in files if f.exists()]
    source = V9["_files"](files)
    frame = con.execute(
        f"SELECT object_id, sensor_type, min(issued_at) AS first FROM read_parquet({source}) "
        f"WHERE sensor_type = {V9['_q'](V9['PHASE'])} AND issued_at < TIMESTAMP {V9['_q'](end)} "
        "GROUP BY 1, 2"
    ).df()
    return frame.set_index(UNIT)["first"]


def add_forecasts(t: dict, first_seen: pd.Series) -> pd.DataFrame:
    """Counts, exposures and the forecasts of every method for the phase cards of a period."""
    cards = t["cards"].copy()
    issued = pd.to_datetime(cards["issued_at"])
    first = pd.to_datetime(
        cards.set_index(UNIT).index.map(first_seen.to_dict()).to_series(index=cards.index)
    ).fillna(issued)
    start = first.where(
        first > issued - pd.Timedelta(days=WINDOW), issued - pd.Timedelta(days=WINDOW)
    )
    cards["n"] = cards["static_list"].to_numpy(dtype=float)
    cards["e"] = ((issued - start) / pd.Timedelta(days=1)).clip(lower=0).to_numpy()
    phase = t["members"][t["members"]["sensor_type"].eq(V9["PHASE"])]
    t_all = issued.to_numpy(dtype="datetime64[ns]")
    # Incident starts of the pair (first events of incidents; an incident is known at its start).
    heads = t["incidents"].loc[t["incidents"]["incident_first"].astype(bool), ["event_id"]]
    pairs = rel.event_pairs(phase, t["events"]).merge(heads, on="event_id")
    head_stamps = {
        k: np.sort(pd.to_datetime(g["event_start"]).to_numpy(dtype="datetime64[ns]"))
        for k, g in pairs.groupby(UNIT, sort=False)
    }
    rate = log(2) / HALF_LIFE
    history = np.datetime64(HISTORY_FROM.to_datetime64(), "ns")
    n_inc = np.zeros(len(cards))
    n_dec = np.zeros(len(cards))
    for key, pos in cards.groupby(UNIT, sort=False).indices.items():
        s = head_stamps.get(key)
        if s is None or not len(s):
            continue
        tt = t_all[pos]
        n_inc[pos] = np.searchsorted(s, tt, "left") - np.searchsorted(
            s, tt - np.timedelta64(WINDOW, "D"), "left"
        )
        s = s[s >= history]
        age = (tt[:, None] - s[None, :]) / DAY
        n_dec[pos] = np.where(age > 0, np.exp(-rate * np.where(age > 0, age, 0.0)), 0.0).sum(axis=1)
    obs_from = first.where(first > HISTORY_FROM, HISTORY_FROM)
    span = ((issued - obs_from) / pd.Timedelta(days=1)).clip(lower=0).to_numpy()
    cards["n_incidents"] = n_inc
    cards["n_decay"] = n_dec
    cards["e_decay"] = (1.0 - np.exp(-rate * span)) / rate
    cards["bayes"] = bayes_by_cutoff(cards, "n_incidents", "e")
    cards["bayes_display"] = displayed(cards["bayes"].to_numpy())
    cards["bayes_decay90"] = bayes_by_cutoff(cards, "n_decay", "e_decay")
    cards["bayes_events"] = bayes_by_cutoff(cards, "n", "e")
    with np.errstate(divide="ignore", invalid="ignore"):
        rate_inc = np.where(cards["e"] > 0, cards["n_incidents"] / cards["e"], 0.0)
    cards["naive"] = -np.expm1(-D * rate_inc)
    return cards


def displayed(p: np.ndarray) -> np.ndarray:
    """The value the card shows: rounded to 5 p.p.; above 0.90 it is «> 90%», coded as
    CAP + STEP / 2 so that it forms its own bin (metrics replace it by the raw mean)."""
    p = np.asarray(p, dtype=float)
    shown = np.round(p / STEP) * STEP
    return np.where(p > CAP, CAP + STEP / 2, np.minimum(shown, CAP))


def display_label(value: float) -> str:
    return "> 90%" if value > CAP else f"{round(value * 100):d}%"


def issued_known(cards: pd.DataFrame, t: dict) -> np.ndarray:
    mask = rel.select_rolling(
        cards, "static_list", k=K, days=D, eligible=t["eligible"], first=t["first"]
    )
    return mask & cards["outcome"].isin(rel.EVALUABLE).to_numpy()


def k_of_n_table(counts: np.ndarray, y: np.ndarray) -> list[tuple]:
    rows = []
    for low, high in KOFN_EDGES:
        inside = (counts >= low) & ((counts <= high) if high is not None else True)
        rows.append((low, high, int(inside.sum()), int(y[inside].sum())))
    return rows


def k_of_n_forecast(counts: np.ndarray, table: list[tuple]) -> np.ndarray:
    out = np.full(len(counts), np.nan)
    for low, high, n, k in table:
        inside = (counts >= low) & ((counts <= high) if high is not None else True)
        out[inside] = k / n if n else np.nan
    return out


# -- calibration metrics -------------------------------------------------------------------


def reliability(p: np.ndarray, y: np.ndarray, bins: int = N_BINS) -> dict:
    """Brier, log loss, ECE and the reliability table on equal-count bins of the forecast."""
    p = np.asarray(p, dtype=float)
    y = np.asarray(y, dtype=float)
    labels = pd.qcut(pd.Series(p).rank(method="dense"), bins, labels=False, duplicates="drop")
    table, ece, inside = [], 0.0, 0
    for b in sorted(pd.unique(labels)):
        sel = (labels == b).to_numpy()
        k, n = int(y[sel].sum()), int(sel.sum())
        w = wilson_interval(k, n)
        mean_p = float(p[sel].mean())
        ok = bool(w["low"] <= mean_p <= w["high"])
        inside += ok
        ece += n / len(p) * abs(k / n - mean_p)
        table.append(
            {
                "forecast_min": float(p[sel].min()),
                "forecast_max": float(p[sel].max()),
                "mean_forecast": mean_p,
                "n": n,
                "k": k,
                "observed": k / n,
                "wilson_low": w["low"],
                "wilson_high": w["high"],
                "inside": ok,
            }
        )
    clipped = np.clip(p, 1e-6, 1 - 1e-6)
    return {
        "cards": len(p),
        "observed_share": float(y.mean()),
        "mean_forecast": float(p.mean()),
        "brier": float(np.mean((p - y) ** 2)),
        "log_loss": float(-np.mean(y * np.log(clipped) + (1 - y) * np.log(1 - clipped))),
        "ece": float(ece),
        "bins": len(table),
        "inside_bins": inside,
        "table": table,
    }


def display_forecast(cards: pd.DataFrame, sel: np.ndarray) -> np.ndarray:
    """Displayed values as numbers for the metrics: «> 90%» → the mean raw forecast of its
    cards."""
    shown = cards.loc[sel, "bayes_display"].to_numpy(dtype=float)
    raw = cards.loc[sel, "bayes"].to_numpy(dtype=float)
    cap = shown > CAP
    out = shown.copy()
    if cap.any():
        out[cap] = raw[cap].mean()
    return out


def evaluate(cards: pd.DataFrame, sel: np.ndarray, table: list[tuple]) -> dict:
    y = cards.loc[sel, "y_card"].to_numpy(dtype=float)
    out = {}
    for method in METHODS:
        if method == "k_of_n":
            p = k_of_n_forecast(cards.loc[sel, "n"].to_numpy(), table)
        elif method == "bayes_display":
            p = display_forecast(cards, sel)
        else:
            p = cards.loc[sel, method].to_numpy(dtype=float)
        out[method] = reliability(p, y)
    return out


def display_table(cards: pd.DataFrame, sel: np.ndarray) -> list[dict]:
    """Per displayed value: «сбылось k из n» with the Wilson interval; shown in the card only
    if the interval contains the value («> 90%»: if its upper bound is above 0.90)."""
    shown = cards.loc[sel, "bayes_display"].to_numpy(dtype=float)
    y = cards.loc[sel, "y_card"].to_numpy(dtype=float)
    rows = []
    for v in sorted(np.unique(shown)):
        m = shown == v
        k, n = int(y[m].sum()), int(m.sum())
        w = wilson_interval(k, n)
        ok = bool(w["high"] > CAP) if v > CAP else bool(w["low"] <= v <= w["high"])
        rows.append(
            {
                "shown": display_label(v),
                "n": n,
                "k": k,
                "observed": k / n,
                "wilson_low": w["low"],
                "wilson_high": w["high"],
                "passes": ok,
            }
        )
    return rows


def ranking_agreement(cards: pd.DataFrame, t: dict) -> dict:
    """If M replaced the list at K = 10: share of the list's issued cards kept."""
    lst = rel.select_rolling(
        cards, "static_list", k=K, days=D, eligible=t["eligible"], first=t["first"]
    )
    bay = rel.select_rolling(cards, "bayes", k=K, days=D, eligible=t["eligible"], first=t["first"])
    by_day = cards.groupby("issued_at", sort=False).indices
    rho = []
    for pos in by_day.values():
        a = cards["static_list"].to_numpy()[pos]
        b = cards["bayes"].to_numpy()[pos]
        if len(pos) > 2 and np.std(a) > 0 and np.std(b) > 0:
            rho.append(pd.Series(a).rank().corr(pd.Series(b).rank()))
    return {
        "same_issued_cards_share": float((lst & bay).sum() / max(lst.sum(), 1)),
        "identical_selection": bool((lst == bay).all()),
        "spearman_within_day_median": float(np.median(rho)) if rho else None,
    }


def prior_summary(cards: pd.DataFrame) -> dict:
    rows = []
    for _, pos in cards.groupby("issued_at", sort=False).indices.items():
        a, b = gamma_prior(cards["n_incidents"].to_numpy()[pos], cards["e"].to_numpy()[pos])
        rows.append((a, b, a / b * D))
    arr = np.array(rows)
    return {
        "alpha_median": float(np.median(arr[:, 0])),
        "beta_median_days": float(np.median(arr[:, 1])),
        "prior_mean_incidents_per_14d_median": float(np.median(arr[:, 2])),
    }


def run(holdout: bool = True) -> dict:
    started = perf_counter()
    con = V9["connect"]()
    periods = ["2023", "2024", *(["holdout"] if holdout else [])]
    data = {}
    for name in periods:
        spec = V9["PERIODS"][name]
        t = PH["phase_tables"](con, name)
        first_seen = pair_first_seen(
            con, V9["LABELS_7D"] if name != "holdout" else spec["labels"], spec["data_end"]
        )
        cards = add_forecasts(t, first_seen)
        data[name] = (t, cards, issued_known(cards, t))
    result = {"dev": {}, "criterion": CRITERION}
    for train, test in DIRECTIONS:
        _, c_tr, s_tr = data[train]
        t_te, c_te, s_te = data[test]
        table = k_of_n_table(
            c_tr.loc[s_tr, "n"].to_numpy(), c_tr.loc[s_tr, "y_card"].to_numpy(dtype=float)
        )
        known = c_te["outcome"].isin(rel.EVALUABLE).to_numpy() & t_te["eligible"]
        result["dev"][f"{train}->{test}"] = {
            "k_of_n_table": [
                {"events_from": a, "events_to": b, "n": n, "k": k} for a, b, n, k in table
            ],
            "issued": evaluate(c_te, s_te, table),
            "all_pair_days": evaluate(c_te, known, table),
            "ranking": ranking_agreement(c_te, t_te),
            "prior": prior_summary(c_te),
        }
    verdict = {}
    for d, block in result["dev"].items():
        kn = block["issued"]["k_of_n"]
        verdict[d] = {}
        for name in ("bayes", "bayes_display"):
            m = block["issued"][name]
            verdict[d][name] = {
                "ece": m["ece"],
                "ece_k_of_n": kn["ece"],
                "inside_bins": m["inside_bins"],
                "bins": m["bins"],
                "pass": bool(
                    m["ece"] <= kn["ece"] and m["inside_bins"] >= CRITERION["inside_bins_min"]
                ),
            }
    result["verdict"] = {
        "per_direction": verdict,
        "pass": all(v[n]["pass"] for v in verdict.values() for n in ("bayes", "bayes_display")),
    }
    dev_cards = pd.concat([data[y][1].loc[data[y][2]] for y in ("2023", "2024")])
    result["display_table_dev"] = display_table(dev_cards, np.ones(len(dev_cards), dtype=bool))
    result["display_table_by_year"] = {
        y: display_table(data[y][1], data[y][2]) for y in ("2023", "2024")
    }
    if holdout:
        t_h, c_h, s_h = data["holdout"]
        table = [(a, b, n, k) for a, b, n, k, _ in PRODUCT_TABLE]
        known = c_h["outcome"].isin(rel.EVALUABLE).to_numpy() & t_h["eligible"]
        result["holdout"] = {
            "use": "SIXTH use of the holdout 2025-07..2026-06: calibration only, descriptive, no "
            "selection (M has no parameters; the decision rests on dev)",
            "k_of_n_table": "the product table (dev 2023 + 2024, 3 bins)",
            "issued": evaluate(c_h, s_h, table),
            "all_pair_days": evaluate(c_h, known, table),
            "display_table": display_table(c_h, s_h),
            "ranking": ranking_agreement(c_h, t_h),
            "prior": prior_summary(c_h),
        }
    result["seconds"] = round(perf_counter() - started, 1)
    return result


# The product «k из n» table (packages/core incident_list.FREQUENCY_BINS, dev 2023 + 2024).
PRODUCT_TABLE = (
    (0, 37, 335, 181, "medium"),
    (38, 62, 264, 202, "high"),
    (63, None, 324, 272, "high"),
)


def main() -> None:
    result = {
        "stage": "v11: probability layer on the phase list (Poisson–gamma empirical Bayes)",
        "protocol": "fixed in the script header before computation; dev decides, holdout "
        "(sixth use) is calibration only",
        **run(),
        **V9["provenance"](),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    V9["write_json"](OUT / "phase_probability_v11.json", result)
    V9["write_json"](REPORT, result)
    print(json.dumps(V9["ev"]._py(result["verdict"]), ensure_ascii=False))
    print(f"report: {REPORT} ({result['seconds']} s)")


if __name__ == "__main__":
    main()

"""Phase: a compact CatBoost against the static list (v10, the last attempt; data analyst task
27.09.2026). Development folds only; the holdout 2025-07..2026-06 is exhausted (five uses) and
is not read. No selection for the product: a success here is only a candidate for a live
check on data after 06.2026.

Pre-registered protocol (fixed in this header before any fit)
---------------------------------------------------------------
- Scope: "Состояние фазы" pairs (object × phase), cards daily at 00:00, horizon 14 days, target
  "all episodes" (labels of exploration 5, as v9).
- Directions: train 2023 → evaluate 2024, and train 2024 → evaluate 2023. Train on the cards
  with a known outcome of the train year; a 14-day embargo drops train cards whose window
  reaches the evaluation year (2023 → 2024) or that start within 14 days after it
  (2024 → 2023).
- Model: CatBoostClassifier, library defaults except ``depth = 6``, ``random_seed = 17``,
  ``verbose = 0``, ``thread_count = 2``; ``cat_features``: the object, the dominant feeder
  kind of the object (B2 dictionary: lighting РО/ГРО/ФРО/ФАО, ventilation ФВ, pumps ФАНС, ОЗК,
  other) and the month of the cutoff.
- Features, strictly from the past (< t), 20 in total with the three categorical ones:
  events of the pair in 7/30/90/365 days; days since the last event; std of the daily event
  series over 30 and 90 days (the rolling means equal count/window and are not repeated);
  lags 1..7 of the daily series; share of the object's phase feeders that had an episode in
  90 days; journal rows "Обесточен" of the object's phase channels in 30 days; day of week.
- Variants: (a) all 20 features; (b) the top 10 by CatBoost importance on the train year,
  refitted on the train year.
- Issue: as v9 — rolling list with release, causal eligibility, K = 10 and K = 7; the score
  is the predicted probability. Reference: the static list 365 days at the same K on the same
  evaluation year and eligibility.
- Criterion of a variant: at some K, in BOTH directions, ΔP ≥ +0.03 and ΔR_incident ≥ 0
  (model − list). R_incident of fresh incidents (no event of the pair in the 14 days before)
  is reported separately without a criterion. Two variants × two K are four looks: a pass is
  a candidate, not a proof.

    python data-science/scripts/phase_catboost_compact_v10.py
"""

from __future__ import annotations

import json
import re
import runpy
from pathlib import Path
from time import perf_counter

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier

from infra_pulse_research.modeling import sensor_failure_release as rel
from infra_pulse_research.modeling.sensor_failure_fe_v7 import pair_event_times

HERE = Path(__file__).resolve().parent
PH = runpy.run_path(str(HERE / "posthoc_sensor_failure_v9_baselines.py"), run_name="ph")
V9 = PH["V9"]
ROOT = V9["ROOT"]
OUT = ROOT / "data-science/artifacts/next-2026-09-27/v10"
REPORT = ROOT / "data-science/reports/sensor-failure-phase-catboost-v10-2026-09-27.json"
CHANNELS = PH["CHANNELS"]
KEYS, UNIT = rel.CARD_KEYS, rel.UNIT
DAYS = 14
KS = (10, 7)
SEED = 17
DIRECTIONS = (("2023", "2024"), ("2024", "2023"))
GOAL = {"delta_precision": 0.03, "delta_recall_incident": 0.0}
TOP_N = 10
JOURNAL_FROM = "2022-01-01"
DAY = np.timedelta64(1, "D")
COUNT_DAYS = (7, 30, 90, 365)
STD_DAYS = (30, 90)
LAGS = tuple(range(1, 8))
CAT_FEATURES = ("object", "feeder_kind", "month")
FEEDER_KINDS = (
    ("lighting", re.compile(r"^\W*(\d+\s*)?(группа\s+)?(г?ро|фро|фао)", re.I)),
    ("ventilation", re.compile(r"^\W*(\d+\s*)?(фидер\s+)?фв", re.I)),
    ("pumps", re.compile(r"^\W*(\d+\s*)?фанс", re.I)),
    ("ozk", re.compile(r"^\W*(\d+\s*)?ф?озк", re.I)),
)
NUMERIC = (
    *(f"events_{d}d" for d in COUNT_DAYS),
    "days_since_last",
    *(f"daily_std_{d}d" for d in STD_DAYS),
    *(f"lag_{k}" for k in LAGS),
    "feeder_share_90d",
    "deenergized_rows_30d",
    "day_of_week",
)
FEATURES = (*NUMERIC, *CAT_FEATURES)


def feeder_kind(name) -> str:
    s = str(name)
    for kind, pattern in FEEDER_KINDS:
        if pattern.search(s):
            return kind
    return "other"


def object_feeders(con) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Phase channels with their class; per object the number of feeders and the dominant
    feeder kind (ties by name order)."""
    names = con.execute(
        f"SELECT channel_id, object_id, name_raw FROM read_parquet({V9['_q'](CHANNELS)}) "
        f"WHERE sensor_type = {V9['_q'](V9['PHASE'])}"
    ).df()
    names["cls"] = names["name_raw"].map(PH["channel_class"])
    feeders = names[names["cls"] == "feeder"].copy()
    feeders["kind"] = feeders["name_raw"].map(feeder_kind)
    count = feeders.groupby("object_id")["channel_id"].nunique()
    kind = (
        feeders.groupby(["object_id", "kind"])
        .size()
        .rename("n")
        .reset_index()
        .sort_values(["object_id", "n", "kind"], ascending=[True, False, True], kind="mergesort")
        .drop_duplicates("object_id")
        .set_index("object_id")["kind"]
    )
    return names, count, kind


def deenergized_daily(end: str) -> pd.DataFrame:
    """Journal rows 'Обесточен' of phase channels per object and day, [JOURNAL_FROM, end)."""
    from infra_pulse_research.modeling.fire_source import open_fire_source  # noqa: PLC0415

    con, _ = open_fire_source(ROOT)
    con.execute("SET threads=2; SET memory_limit='4GB'")
    last_month = (pd.Timestamp(end) - pd.Timedelta(days=1)).strftime("%Y-%m")
    df = con.execute(
        f"""SELECT object_id, CAST(TRY_CAST(event_ts_local_raw AS TIMESTAMP) AS DATE) AS day,
                  count(*) AS n
            FROM working_events
            WHERE sensor_type = {V9["_q"](V9["PHASE"])} AND value_raw = 'Обесточен'
              AND month BETWEEN {V9["_q"](JOURNAL_FROM[:7])} AND {V9["_q"](last_month)}
              AND TRY_CAST(event_ts_local_raw AS TIMESTAMP) >= TIMESTAMP {V9["_q"](JOURNAL_FROM)}
              AND TRY_CAST(event_ts_local_raw AS TIMESTAMP) < TIMESTAMP {V9["_q"](end)}
              AND object_id IS NOT NULL
            GROUP BY 1, 2"""
    ).df()
    con.close()
    return df


def _window_sum(cum: np.ndarray, idx: np.ndarray, days: int) -> np.ndarray:
    """Sum of a daily series over days [idx − days, idx) from its prefix sums."""
    lo = np.clip(idx - days, 0, len(cum) - 1)
    hi = np.clip(idx, 0, len(cum) - 1)
    return cum[hi] - cum[lo]


def pair_features(cards: pd.DataFrame, members: pd.DataFrame) -> pd.DataFrame:
    """Counters, recency, daily std and lags of the pair's event series (strictly < t)."""
    phase = members[members["sensor_type"].eq(V9["PHASE"])]
    times = pair_event_times(phase, None)
    origin = np.datetime64(JOURNAL_FROM, "D")
    t = pd.to_datetime(cards["issued_at"]).to_numpy(dtype="datetime64[ns]")
    t_day = t.astype("datetime64[D]")
    idx_all = ((t_day - origin) / DAY).astype(int)
    n_days = int(idx_all.max()) + 2
    object_level = ("feeder_share_90d", "deenergized_rows_30d")
    out = {c: np.zeros(len(cards)) for c in NUMERIC if c not in object_level}
    out["days_since_last"] = np.full(len(cards), np.nan)
    stamps = {
        k: np.sort(g["count_at"].to_numpy(dtype="datetime64[ns]"))
        for k, g in times.groupby(UNIT, sort=False)
    }
    for key, pos in cards.groupby(UNIT, sort=False).indices.items():
        s = stamps.get(key, np.array([], dtype="datetime64[ns]"))
        s = s[s >= origin.astype("datetime64[ns]")]
        day_idx = ((s.astype("datetime64[D]") - origin) / DAY).astype(int)
        daily = np.bincount(day_idx[day_idx < n_days], minlength=n_days).astype(float)
        cum = np.concatenate([[0.0], np.cumsum(daily)])
        cum2 = np.concatenate([[0.0], np.cumsum(daily * daily)])
        idx = idx_all[pos]
        tt = t[pos]
        for d in COUNT_DAYS:
            # Counts by the exact moment (< t) — midnight cutoffs make it equal to days.
            lo = np.searchsorted(s, tt - np.timedelta64(d, "D"), "left")
            hi = np.searchsorted(s, tt, "left")
            out[f"events_{d}d"][pos] = hi - lo
        last = np.searchsorted(s, tt, "left")
        has = last > 0
        if len(s):
            prev = s[np.maximum(last - 1, 0)]
            out["days_since_last"][pos] = np.where(has, (tt - prev) / DAY, np.nan)
        for d in STD_DAYS:
            mean = _window_sum(cum, idx, d) / d
            sq = _window_sum(cum2, idx, d) / d
            out[f"daily_std_{d}d"][pos] = np.sqrt(np.maximum(sq - mean * mean, 0.0))
        for k in LAGS:
            j = idx - k
            out[f"lag_{k}"][pos] = np.where(j >= 0, daily[np.clip(j, 0, n_days - 1)], 0.0)
    out["day_of_week"] = pd.to_datetime(cards["issued_at"]).dt.dayofweek.to_numpy(dtype=float)
    return pd.DataFrame(out, index=cards.index)


def object_features(cards, members, feeder_count, feeder_kind_of, deenergized) -> pd.DataFrame:
    """Feeder share with an episode in 90 days, 'Обесточен' rows in 30 days, feeder kind,
    month and the object as categories."""
    phase = members[members["sensor_type"].eq(V9["PHASE"])][["object_id", "channel_id", "start_at"]]
    phase = phase.assign(start_at=pd.to_datetime(phase["start_at"]))
    t = pd.to_datetime(cards["issued_at"])
    share = np.zeros(len(cards))
    rows = np.zeros(len(cards))
    de = deenergized.assign(day=pd.to_datetime(deenergized["day"]))
    for obj, pos in cards.groupby("object_id", sort=False).indices.items():
        p = phase[phase["object_id"] == obj]
        n_feeders = feeder_count.get(obj, np.nan)
        starts = p["start_at"].to_numpy(dtype="datetime64[ns]")
        chans = p["channel_id"].to_numpy()
        d = de[de["object_id"] == obj]
        days = d["day"].to_numpy(dtype="datetime64[ns]")
        counts = d["n"].to_numpy(dtype=float)
        for i in pos:
            ti = t.iloc[i].to_datetime64()
            recent = (starts < ti) & (starts >= ti - np.timedelta64(90, "D"))
            share[i] = len(set(chans[recent])) / n_feeders if n_feeders else np.nan
            window = (days < ti) & (days >= ti - np.timedelta64(30, "D"))
            rows[i] = counts[window].sum()
    return pd.DataFrame(
        {
            "feeder_share_90d": share,
            "deenergized_rows_30d": rows,
            "object": cards["object_id"].astype(str).to_numpy(),
            "feeder_kind": cards["object_id"].map(feeder_kind_of).fillna("other").to_numpy(),
            "month": t.dt.month.astype(str).to_numpy(),
        },
        index=cards.index,
    )


def build(con) -> dict:
    """Phase tables of 2023 and 2024 (v9 loaders) with the v10 features."""
    tables = {y: PH["phase_tables"](con, y) for y in ("2023", "2024")}
    _, feeder_count, kind = object_feeders(con)
    deenergized = deenergized_daily(V9["DEV_END"])
    for t in tables.values():
        cards = t["cards"]
        feats = pair_features(cards, t["members"]).join(
            object_features(cards, t["members"], feeder_count, kind, deenergized)
        )
        t["features"] = feats[list(FEATURES)]
    return tables


def train_mask(cards: pd.DataFrame, train: str, test: str) -> np.ndarray:
    """Known outcome, window not beyond the data, 14-day embargo at the evaluation year."""
    issued = pd.to_datetime(cards["issued_at"])
    known = cards["outcome"].isin(rel.EVALUABLE).to_numpy()
    if int(train) < int(test):
        boundary = pd.Timestamp(f"{test}-01-01")
        keep = (issued + pd.Timedelta(days=DAYS) <= boundary).to_numpy()
    else:
        boundary = pd.Timestamp(f"{train}-01-01")
        keep = (issued >= boundary + pd.Timedelta(days=DAYS)).to_numpy()
    return known & keep


def fit(x: pd.DataFrame, y: np.ndarray, features: list[str]) -> CatBoostClassifier:
    model = CatBoostClassifier(
        depth=6, random_seed=SEED, verbose=0, thread_count=2, allow_writing_files=False
    )
    cats = [c for c in features if c in CAT_FEATURES]
    model.fit(x[features], y, cat_features=cats)
    return model


def evaluate(t: dict, score: np.ndarray, k: int, fast) -> dict:
    cards = t["cards"].assign(_score=score)
    mask = rel.select_rolling(
        cards, "_score", k=k, days=DAYS, eligible=t["eligible"], first=t["first"]
    )
    return fast.metrics(mask)


def run() -> dict:
    started = perf_counter()
    con = V9["connect"]()
    tables = build(con)
    fast = {
        y: PH["FastEvaluator"](t["cards"], t["links"], t["events"], t["first"], t["incidents"])
        for y, t in tables.items()
    }
    results, importances = {}, {}
    for train, test in DIRECTIONS:
        tr, te = tables[train], tables[test]
        m = train_mask(tr["cards"], train, test)
        x, y = tr["features"][m], tr["cards"].loc[m, "y_card"].to_numpy(dtype=int)
        full = fit(x, y, list(FEATURES))
        imp = pd.Series(full.get_feature_importance(), index=list(FEATURES)).sort_values(
            ascending=False, kind="mergesort"
        )
        top = list(imp.index[:TOP_N])
        topm = fit(x, y, top)
        importances[f"{train}->{test}"] = {
            "all": {k: float(v) for k, v in imp.items()},
            "top10": top,
        }
        scores = {
            "catboost_all": full.predict_proba(te["features"][list(FEATURES)])[:, 1],
            "catboost_top10": topm.predict_proba(te["features"][top])[:, 1],
        }
        block = {"train_cards": int(m.sum()), "train_positive_share": float(y.mean())}
        for k in KS:
            ref = evaluate(te, te["cards"]["static_list"].to_numpy(dtype=float), k, fast[test])
            row = {"static_list": ref}
            for name, sc in scores.items():
                got = evaluate(te, sc, k, fast[test])
                got["delta_precision"] = got["card_precision"] - ref["card_precision"]
                got["delta_recall_incident"] = got["R_incident"] - ref["R_incident"]
                got["delta_recall_fresh"] = got["R_incident_fresh"] - ref["R_incident_fresh"]
                row[name] = got
            block[f"K{k}"] = row
        results[f"{train}->{test}"] = block
        print(f"{train}->{test} done ({round(perf_counter() - started)} s)", flush=True)
    verdict = {}
    for name in ("catboost_all", "catboost_top10"):
        per_k = {}
        for k in KS:
            ok = all(
                results[d][f"K{k}"][name]["delta_precision"] >= GOAL["delta_precision"]
                and results[d][f"K{k}"][name]["delta_recall_incident"]
                >= GOAL["delta_recall_incident"]
                for d in results
            )
            per_k[f"K{k}"] = ok
        verdict[name] = {"per_k": per_k, "pass": any(per_k.values())}
    return {
        "stage": "v10: compact CatBoost on the phase vs the static list (development folds)",
        "note": "holdout exhausted (five uses) and not read; a pass is only a candidate for a "
        "live check on data after 06.2026; criterion fixed in the script header before the fit",
        "features": list(FEATURES),
        "cat_features": list(CAT_FEATURES),
        "n_features": len(FEATURES),
        "model": {"library": "catboost", "depth": 6, "random_seed": SEED, "other": "defaults"},
        "criterion": GOAL,
        "results": results,
        "importances": importances,
        "verdict": verdict,
        "seconds": round(perf_counter() - started, 1),
        **V9["provenance"](),
    }


def main() -> None:
    result = run()
    OUT.mkdir(parents=True, exist_ok=True)
    V9["write_json"](OUT / "phase_catboost_v10.json", result)
    V9["write_json"](REPORT, result)
    print(json.dumps(result["verdict"], ensure_ascii=False))
    print(f"report: {REPORT} ({result['seconds']} s)")


if __name__ == "__main__":
    main()

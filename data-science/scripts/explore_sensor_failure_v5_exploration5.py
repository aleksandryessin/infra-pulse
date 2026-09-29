"""Exploration 5 of the v5 sensor-failure list (team decision 27.09.2026). No training;
development folds 2023/2024 only; the holdout is not read (it has been used 4 times).
Moved into the repository on 28.09.2026 (it was run from outside it on 27.09): the labels it
reads are the dev-stage labels of v9 (FINAL_V9.md). Outputs stay git-ignored in
data-science/artifacts/next-2026-09-27/ds-exploration5 (or $EXPLORATION5_OUT). Labels:

    uv run --locked --group train --group research \
        python data-science/scripts/measure_sensor_failure.py \
        --model-labels --label-months development \
        --model-config configs/sensor_failure_v5_exploration5_labels.json \
        --output data-science/artifacts/next-2026-09-27/ds-exploration5 --labels-dir labels \
        --tables data-science/artifacts/sensor-failure-v1/tables

Scopes of sensor types, each with its own list, its own events and its own ceiling:
S0 all; S1 without rare types; S2 fire system; S3 dispatcher control; S4 "sensors of the
ТЗ" (smoke, heat, temperature, gas, door, AV contact, motion); and the S4 layers
(gas, fire, security, temperature). Horizons 14..21 days (K from 8 to D; S4 and layers
from 3) plus 7 days at K 3/5/7; policies release and keep; scores: static list, list +
persistence (rank mean), 90-day decay, Bayesian Poisson-gamma of the pair (candidate 5 of
v6, ported without gradient training), ceiling. Honest recall only.

    python data-science/scripts/explore_sensor_failure_v5_exploration5.py --target all
    python data-science/scripts/explore_sensor_failure_v5_exploration5.py --target ge2s
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
from math import log
from pathlib import Path
from time import perf_counter

import duckdb
import numpy as np
import pandas as pd

from infra_pulse_research.modeling import sensor_failure_evaluation as ev
from infra_pulse_research.modeling.sensor_failure_nodes import select_v4
from infra_pulse_research.modeling.sensor_failure_tuning import day_rank

ROOT = Path(__file__).resolve().parents[2]
P4 = runpy.run_path(
    str(ROOT / "data-science/scripts/explore_sensor_failure_v5_part4.py"), run_name="p4"
)
P2, X, V5 = P4["P2"], P4["X"], P4["V5"]
OUT = Path(
    os.environ.get(
        "EXPLORATION5_OUT", ROOT / "data-science/artifacts/next-2026-09-27/ds-exploration5"
    )
)
LABELS = OUT / "labels"
LABELS_7D = V5["SF"] / "model_labels_v5"
TARGETS = {"all": V5["THRESHOLDS"]["all"], "ge2s": V5["THRESHOLDS"]["ge2s"]}
HORIZONS = (7, 14, 15, 16, 17, 18, 19, 20, 21)
K7 = (3, 5, 7)
YEARS = (2023, 2024)
KEYS = ["object_id", "sensor_type", "issued_at"]
UNIT = ["object_id", "sensor_type"]
RARE = {
    "ИБП",
    "Состояние охраны",
    "Датчик затопления",
    "КД Люк",
    "Стекло",
    "9-секционный люк",
    "Ручной извещатель",
}
TZ = {
    "Датчик дыма",
    "Тепловой датчик",
    "Датчик температуры",
    "Газовый датчик",
    "КД Дверь",
    "КД АВ",
    "Датчик движения",
}
LAYERS = {
    "L_gas": {"Газовый датчик"},
    "L_fire": {"Датчик дыма", "Тепловой датчик"},
    "L_security": {"КД Дверь", "КД АВ", "Датчик движения"},
    "L_temp": {"Датчик температуры"},
}
SCOPES = ("S0", "S1", "S2", "S3", "S4", *LAYERS)
K_FROM = {"S0": 8, "S1": 8, "S2": 8, "S3": 8, "S4": 3, **{k: 3 for k in LAYERS}}
SCORES = ("static_list", "list+persistence", "decay_90", "bayes_pg")
POLICIES = ("release", "keep")
HALF_LIFE = 90.0


def scope_mask(cards: pd.DataFrame, scope: str) -> np.ndarray:
    st, sy = cards["sensor_type"], cards["system_type"]
    if scope == "S0":
        m = pd.Series(True, index=cards.index)
    elif scope == "S1":
        m = ~st.isin(RARE)
    elif scope == "S2":
        m = sy.eq("Пожарная охрана")
    elif scope == "S3":
        m = sy.eq("Диспетчерский контроль")
    elif scope == "S4":
        m = st.isin(TZ)
    else:
        m = st.isin(LAYERS[scope])
    return m.to_numpy()


def budgets(scope: str, days: int) -> list[int]:
    if days == 7:
        return list(K7)
    return list(range(K_FROM[scope], days + 1))


def connect() -> duckdb.DuckDBPyConnection:
    tmp = OUT / "tmp" / str(os.getpid())
    tmp.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET threads=6; SET memory_limit='6GB'; SET temp_directory={V5['_q'](tmp)}")
    return con


# -- Bayesian Poisson-gamma of the pair: ported from scripts/tune_sensor_failure_v6.py
# (candidate 5), no gradient training: a gamma prior per sensor type by the method of
# moments on the history before the fold boundary, decayed counts and exposure.

ORIGIN = pd.Timestamp("2019-01-01")
DAY = pd.Timedelta(1, "D")
GAP = (
    float((pd.Timestamp("2021-01-01") - ORIGIN) / DAY),
    float((pd.Timestamp("2022-01-01") - ORIGIN) / DAY),
)
MIN_PRIOR_DAYS = 30.0


def to_days(values) -> np.ndarray:
    return ((pd.to_datetime(pd.Series(values)) - ORIGIN) / DAY).to_numpy(dtype=float)


def observed_parts(start: float, end: float) -> list[tuple[float, float]]:
    parts = []
    for lo, hi in ((-np.inf, GAP[0]), (GAP[1], np.inf)):
        a, b = max(start, lo), min(end, hi)
        if b > a:
            parts.append((a, b))
    return parts


def decayed_sum(event_days: np.ndarray, t: np.ndarray, rate: float) -> np.ndarray:
    t = np.asarray(t, dtype=float)
    if not len(event_days):
        return np.zeros(len(t))
    age = t[:, None] - np.asarray(event_days, dtype=float)[None, :]
    past = age > 0
    return np.where(past, np.exp(-rate * np.where(past, age, 0.0)), 0.0).sum(axis=1)


def decayed_exposure(start: float, t: np.ndarray, rate: float) -> np.ndarray:
    t = np.asarray(t, dtype=float)
    out = np.zeros(len(t))
    for lo, hi in ((-np.inf, GAP[0]), (GAP[1], np.inf)):
        a = max(start, lo)
        b = np.minimum(t, hi)
        ok = b > a
        value = (np.exp(-rate * (t - np.where(ok, b, t))) - np.exp(-rate * (t - a))) / rate
        out += np.where(ok, value, 0.0)
    return out


def gamma_prior(counts, exposures):
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


def pair_starts(con) -> pd.DataFrame:
    files = V5["_files"](
        LABELS_7D / f"cards/connection_loss_all/168h/{m}.parquet" for m in V5["DEV_MONTHS"]
    )
    frame = con.execute(
        f"SELECT object_id, sensor_type, min(issued_at) AS first_card FROM read_parquet({files}) "
        f"WHERE issued_at < TIMESTAMP {V5['_q'](V5['DEV_END'])} GROUP BY 1, 2 ORDER BY 1, 2"
    ).df()
    frame["start"] = to_days(frame["first_card"])
    return frame


def fit_bayes(times: pd.DataFrame, starts: pd.DataFrame, boundary) -> dict:
    end_d = float(to_days([boundary])[0])
    days = times.assign(_d=to_days(times["count_at"]))
    days = days[days["_d"] < end_d]
    grouped = {k: np.sort(g["_d"].to_numpy()) for k, g in days.groupby(UNIT, sort=False)}
    counts, exposure, types = [], [], []
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
        counts.append(int(inside.sum()))
        exposure.append(float(sum(b - a for a, b in parts)))
        types.append(sensor)
    counts, exposure, types = (
        np.array(counts, float),
        np.array(exposure, float),
        np.array(types, object),
    )
    pooled = gamma_prior(counts, exposure)
    if pooled is None:
        m = counts.sum() / exposure.sum()
        pooled = (1.0, 1.0 / m)
    params = {"_pooled": pooled}
    for sensor in sorted(set(types)):
        own = types == sensor
        params[str(sensor)] = gamma_prior(counts[own], exposure[own]) or pooled
    return params


def bayes_scores(cards, times, starts, params, days: int) -> np.ndarray:
    rate = log(2) / HALF_LIFE
    events = {k: np.sort(to_days(g["count_at"])) for k, g in times.groupby(UNIT, sort=False)}
    first = {
        (o, s): v
        for o, s, v in zip(starts.object_id, starts.sensor_type, starts.start, strict=True)
    }
    issued = to_days(cards["issued_at"])
    out = np.full(len(cards), np.nan)
    for key, positions in cards.groupby(UNIT, sort=False).indices.items():
        t = issued[positions]
        alpha, beta = params.get(str(key[1]), params["_pooled"])
        a = alpha + decayed_sum(events.get(key, np.array([])), t, rate)
        b = beta + decayed_exposure(first.get(key, float(t.min())), t, rate)
        out[positions] = -np.expm1(a * np.log(b / (b + days)))
    return out


# -- loading -----------------------------------------------------------------------------


def load_fold(con, target: str, days: int, year: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    root = LABELS_7D if days == 7 else LABELS
    out = []
    for kind in ("cards", "links"):
        files = V5["_files"](
            root / f"{kind}/{target}/{days * 24}h/{m}.parquet" for m in V5["year_months"](year)
        )
        out.append(
            con.execute(
                f"SELECT * FROM read_parquet({files}) "
                f"WHERE issued_at < TIMESTAMP {V5['_q'](V5['DEV_END'])}"
            ).df()
        )
    return out[0].reset_index(drop=True), out[1]


def events_members(con, target: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    q = V5["_q"]
    base = LABELS / "events" / target
    cut = f"TIMESTAMP {q(V5['DEV_END'])}"
    events = con.execute(
        f"SELECT * FROM read_parquet({q(base / 'events.parquet')}) WHERE event_start < {cut}"
    ).df()
    members = con.execute(
        f"SELECT * FROM read_parquet({q(base / 'event_members.parquet')}) WHERE start_at < {cut}"
    ).df()
    return events, members


# -- metrics -------------------------------------------------------------------------------


def point(cards, links, events, mask, days, keep, prev, layer_of_event=None) -> dict:
    evaluable = cards["outcome"].isin(ev.EVALUABLE).to_numpy()
    y = cards["y_card"].to_numpy(dtype=float)
    chosen = mask & evaluable
    h = P4["honest_outcomes"](cards, links, events, mask, days, keep)
    caught = h[h["captured_open"]]
    lead = caught["lead_open"].to_numpy(dtype=float)
    per_day = pd.Series(mask.astype(int)).groupby(cards["issued_at"].to_numpy()).sum()
    repeat = caught["repeat"].astype(bool)
    out = {
        "cards_selected": int(chosen.sum()),
        "card_precision": float(y[chosen].mean()) if chosen.any() else None,
        "event_recall_open": float(h["captured_open"].mean()) if len(h) else None,
        "event_recall_without_repeats": float(
            (h["captured_open"] & ~h["repeat"].fillna(False).astype(bool)).mean()
        )
        if len(h)
        else None,
        "new_cards_per_day": float(per_day.mean()),
        "lead_hours_median_open": float(np.median(lead)) if len(lead) else None,
        "chronic_share_issued": float(prev[chosen].mean()) if chosen.any() else None,
        "repeat_share_captured": float(repeat.mean()) if len(caught) else None,
    }
    if layer_of_event is not None:
        layers = {}
        st = cards["sensor_type"]
        for name, types in LAYERS.items():
            lm = chosen & st.isin(types).to_numpy()
            ids = layer_of_event.get(name, set())
            hl = h[h["event_id"].isin(ids)]
            layers[name] = {
                "cards_selected": int(lm.sum()),
                "card_precision": float(y[lm].mean()) if lm.any() else None,
                "events": len(hl),
                "event_recall_open": float(hl["captured_open"].mean()) if len(hl) else None,
            }
        out["layers"] = layers
    return out


def summarize(folds: list[dict]) -> dict:
    keys = [k for k in folds[0] if k not in ("cards_selected", "layers")]
    mean = {
        k: float(np.mean([f[k] for f in folds]))
        for k in keys
        if all(f[k] is not None for f in folds)
    }
    p = [f["card_precision"] for f in folds]
    r = [f["event_recall_open"] for f in folds]
    ok = all(v is not None for v in p + r)
    min_p = min(p) if ok else None
    min_r = min(r) if ok else None
    gap = float(np.hypot(max(0.0, 0.7 - min_p), max(0.0, 0.5 - min_r))) if ok else None
    return {
        "folds": folds,
        "mean": mean,
        "min_precision": min_p,
        "min_recall_open": min_r,
        "gap_worst_fold": gap,
    }


def scope_stats(cards, links, events) -> dict:
    evaluable = cards["outcome"].isin(ev.EVALUABLE).to_numpy()
    out = ev.event_outcomes(cards, links, events, np.zeros(len(cards), dtype=bool))
    return {
        "cards_evaluable": int(evaluable.sum()),
        "base_rate": float(cards.loc[evaluable, "y_card"].mean()) if evaluable.any() else None,
        "events_evaluable": int(out["evaluable"].sum()),
        "pairs": int(cards.loc[evaluable, UNIT].drop_duplicates().shape[0]),
    }


def run(target_name: str) -> dict:
    started = perf_counter()
    target, rule = TARGETS[target_name]
    con = connect()
    events, members = events_members(con, target)
    persistence = {y: V5["channel_persistence"](con, V5["year_months"](y)) for y in YEARS}
    times_all = P2["unit_event_times"](P2["without_year"](members), rule)
    starts = pair_starts(con)
    priors = {y: fit_bayes(times_all, starts, pd.Timestamp(f"{y}-01-01")) for y in YEARS}
    result = {"target": target, "points": [], "scopes": {}, "combinations": 0}
    for days in HORIZONS:
        folds = []
        for year in YEARS:
            cards, links = load_fold(con, target, days, year)
            cards = ev.attach_card_scores(cards, persistence[year], ["persistence"])
            cards["static_list"] = ev.static_list_scores(cards, members, rule).to_numpy()
            if not np.array_equal(
                P2["window_counts"](cards, times_all, 365), cards["static_list"].to_numpy()
            ):
                raise ValueError("flat list rebuilt from the variant history differs")
            cards["decay_90"] = P2["decayed_counts"](cards, times_all, 90)
            cards["bayes_pg"] = bayes_scores(cards, times_all, starts, priors[year], days)
            prev = ev.static_list_scores(cards, members, rule, window_days=days).to_numpy() > 0
            folds.append((cards, links, prev))
        for scope in SCOPES:
            parts = []
            for cards, links, prev in folds:
                m = scope_mask(cards, scope)
                sub = cards[m].reset_index(drop=True)
                keys = sub[KEYS].drop_duplicates()
                lk = links.merge(keys, on=KEYS, how="inner")
                sub["list+persistence"] = 0.5 * (
                    day_rank(sub, "static_list") + day_rank(sub, "persistence")
                )
                sub["ceiling"] = X["oracle"](sub, lk, events)
                layer_of_event = None
                if scope == "S4":
                    typed = lk.merge(sub[[*KEYS]].assign(_t=sub["sensor_type"]), on=KEYS)
                    layer_of_event = {
                        name: set(typed.loc[typed["_t"].isin(types), "event_id"])
                        for name, types in LAYERS.items()
                    }
                parts.append((sub, lk, prev[m], layer_of_event))
            result["scopes"].setdefault(scope, {})[f"{days}d"] = [
                scope_stats(s, lk, events) for s, lk, _, _ in parts
            ]
            for kind in POLICIES:
                keep = kind == "keep"
                for score in (*SCORES, "ceiling"):
                    for k in budgets(scope, days):
                        policy = P4["policy"](kind, days, k)
                        per_fold = [
                            point(s, lk, events, select_v4(s, score, policy), days, keep, pv, lay)
                            for s, lk, pv, lay in parts
                        ]
                        entry = summarize(per_fold)
                        entry.update(
                            {"scope": scope, "days": days, "policy": kind, "score": score, "k": k}
                        )
                        result["points"].append(entry)
                        if score != "ceiling":
                            result["combinations"] += 1
            print(
                f"{target_name} {days}d {scope} ({round(perf_counter() - started)} s)", flush=True
            )
    result["seconds"] = round(perf_counter() - started, 1)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=list(TARGETS), required=True)
    args = parser.parse_args()
    result = run(args.target)
    text = json.dumps(ev._py(result), ensure_ascii=False)
    if any(k in text for k in ('"object_id"', '"channel_id"', '"event_id"', '"node_id"')):
        raise ValueError("output must not contain identifiers")
    (OUT / f"points_{args.target}.json").write_text(text, encoding="utf-8")
    print(
        f"written points_{args.target}.json "
        f"({result['seconds']} s, {result['combinations']} combinations)"
    )


if __name__ == "__main__":
    main()

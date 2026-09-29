"""v9 of the sensor-failure list (Head DS task 27.09.2026 after the independent audit).

Phase 1 (``--stage dev``): development folds 2023 and 2024 only; the holdout
2025-07..2026-06 is never read (its labels are not opened, every query of events stops at
2025-01-01). No training.

- One release rule (``modeling/sensor_failure_release.py``): an issued card leaves the list
  at the start of the first event of its pair; causal selection by default (a card is
  eligible only by what is known at the cutoff, including the covered lookback).
- Three recall definitions side by side: ``R_repeats`` (any event with an open card, for
  reference), ``R_strict`` (only the first event of an open card), ``R_incident`` (events
  of a pair less than 24 h apart, chained, form one incident; caught if a card is open at
  the start of its first event). Precision is per card and does not change.
- Scopes: ``phase`` (production: "Состояние фазы" only), ``S3`` (phase + flood, reference),
  demo ``S1`` (without rare types), ``S4`` (sensors of the ТЗ) and ``fire`` (smoke + heat).
- Horizons 7, 14 and 21 days; release only; scores: static list 365 days, 90-day decay,
  Bayesian Poisson-gamma of the pair (v6 candidate 5); greedy oracles as ceilings; K grid of
  exploration 5 (phase: 1..15).
- Demo scopes S1, S4 and fire also WITH the TO-session mask of smoke and heat detectors
  (``infra_pulse_research.sensor_failure_maintenance``, DS-A; journal 2022-01..2024-12) and
  the demo target "fire detector failure" (a + b1) on the unit object × fire subsystem, 14
  days, static list (``modeling/sensor_failure_service_mask.py``).
- The selection rule of the task (``choose_prod`` / ``choose_demo``) for ``main_recall`` =
  ``R_incident`` (confirmed) and ``R_strict``; a draft (not frozen)
  ``configs/sensor_failure_final_v9.json`` with ``--write-draft-config``; a k-of-n frequency
  calibration check of the production point on the folds.

Local outputs with no identifiers go to ``artifacts/next-2026-09-27/v9`` (git-ignored);
the aggregated dev report is ``reports/sensor-failure-final-v9-2026-09-27.json``.

    python data-science/scripts/final_sensor_failure_v9.py --stage dev --write-draft-config

Phase 2 (after the written confirmation of Head DS): ``--stage holdout --dry-run`` runs the
holdout code with 2023/2024 in the role of the holdout and must reproduce the dev points
exactly; ``--stage freeze`` writes the frozen config (``FROZEN_PLAN``) and
``artifacts/next-2026-09-27/v9/freeze.log`` (time, SHA-256 of the config and of the code);
``--stage holdout`` checks the log, recomputes the folds, then reads the holdout period once
(fifth use), with week and object block bootstrap and the k-of-n calibration against dev.
On a crash: fix, ``--stage repeat-fix`` (new hashes), ``--stage holdout --repeat`` once.

    python data-science/scripts/final_sensor_failure_v9.py --stage holdout --dry-run
    python data-science/scripts/final_sensor_failure_v9.py --stage freeze
    python data-science/scripts/final_sensor_failure_v9.py --stage holdout
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import subprocess
from pathlib import Path
from time import perf_counter

import duckdb
import numpy as np
import pandas as pd

from infra_pulse_research.modeling import sensor_failure_evaluation as ev
from infra_pulse_research.modeling import sensor_failure_release as rel
from infra_pulse_research.modeling import sensor_failure_service_mask as sm
from infra_pulse_research.modeling.subsystem_evaluation import _interval
from infra_pulse_research.modeling.target_audit import wilson_interval

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SF = ROOT / "data-science/artifacts/sensor-failure-v1"
E5_LABELS = ROOT / "data-science/artifacts/next-2026-09-27/ds-exploration5/labels"
LABELS_7D = SF / "model_labels_v5"
OUT = ROOT / "data-science/artifacts/next-2026-09-27/v9"
CONFIG = ROOT / "data-science/configs/sensor_failure_final_v9.json"
REPORT = ROOT / "data-science/reports/sensor-failure-final-v9-2026-09-27.json"
COVERAGE = ROOT / "data-science/artifacts/curated-all-sensors-exclude-2021-q2/coverage.parquet"
POLICY_DAYS = ROOT / "data-science/artifacts/alarm-spike-exclusions-v1/policy_days.parquet"
TARGET, RULE = "connection_loss_all", None
DEV_END = "2025-01-01"
DEV_MONTHS = [f"{y}-{m:02d}" for y in (2019, 2020, 2022, 2023, 2024) for m in range(1, 13)]
FOLDS = {
    2023: ("2023-01-01", "2024-01-01", None),
    2024: ("2024-01-01", "2025-01-01", "2025-01-01"),
}
HORIZONS = (7, 14, 21)
SCORES = ("static_list", "decay_90", "bayes_pg")
CEILINGS = ("ceiling", "ceiling_incident")
HALF_LIFE = 90.0
KEYS = rel.CARD_KEYS
UNIT = rel.UNIT
PHASE = "Состояние фазы"
DISPATCH = "Диспетчерский контроль"
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
FIRE = {"Датчик дыма", "Тепловой датчик"}
SCOPES = ("phase", "S3", "S1", "S4", "fire")
SCOPE_TEXT = {
    "phase": "production: 'Состояние фазы' only ('Неисправен' = loss of input power)",
    "S3": "reference: 'Диспетчерский контроль' (phase + flood)",
    "S1": "demo: all types without UPS, flood, КД Люк, glass, manual call point",
    "S4": "demo: sensors of the ТЗ (smoke, heat, temperature, gas, КД Дверь, КД АВ, motion)",
    "fire": "demo: fire layer (smoke + heat)",
}
PROD_RULE = {
    "scope": "phase",
    "days": (7, 14),
    "score": "static_list",
    "min_precision": 0.74,
    "min_recall": 0.50,
    "neighbour_min_precision": 0.72,
    "fallback_days": 14,
}
DEMO_RULE = {"scopes": ("S1", "S4", "fire"), "days": 14, "score": "static_list"}
MAIN_RECALLS = ("R_incident", "R_strict")
MASKED_SCOPES = ("S1", "S4", "fire")
MASK_SUFFIX = "+mask"
FIRE_TARGET = "fire_detector"
FIRE_TARGET_K = 5
DEMO_DAYS = 14
MASK_VERSION = sm.fm.DEFINITION_VERSION
JOURNAL_FROM = "2022-01-01"
DEMO_MASK_RULE = {
    "scopes": tuple(f"{s}{MASK_SUFFIX}" for s in MASKED_SCOPES),
    "days": DEMO_DAYS,
    "score": "static_list",
}
CALIBRATION_MIN_N = 30


def base_scope(scope: str) -> str:
    return scope.removesuffix(MASK_SUFFIX)


def scope_mask(frame: pd.DataFrame, scope: str) -> np.ndarray:
    """Rows (cards or members) of a scope by ``sensor_type`` / ``system_type``."""
    scope = base_scope(scope)
    st = frame["sensor_type"]
    if scope == "phase":
        m = st.eq(PHASE)
    elif scope == "S3":
        m = frame["system_type"].eq(DISPATCH)
    elif scope == "S1":
        m = ~st.isin(RARE)
    elif scope == "S4":
        m = st.isin(TZ)
    elif scope == "fire":
        m = st.isin(FIRE)
    else:
        raise ValueError(f"unknown scope: {scope}")
    return m.to_numpy()


def budgets(scope: str, days: int) -> list[int]:
    """K grid of exploration 5; the production scope gets 1..15 at every horizon."""
    scope = base_scope(scope)
    if scope == "phase":
        return list(range(1, 16))
    if scope == FIRE_TARGET:
        return list(range(1, days + 1))
    if days == 7:
        return [3, 5, 7]
    start = 8 if scope in ("S1", "S3") else 3
    return list(range(start, days + 1))


# -- selection rule ---------------------------------------------------------------------


def _min(point: dict, metric: str):
    return point.get(f"min_{metric}")


def rule_passing(rows: list[dict], main_recall: str, rule: dict = PROD_RULE) -> list[dict]:
    """Points with min P >= 0.74 and min R_main >= 0.50 on the worse fold whose neighbours
    K ± 1 of the same horizon and score (those in the grid) keep min P >= 0.72."""
    by = {(p["days"], p["score"], p["k"]): p for p in rows}
    passing = []
    for p in rows:
        ok = _min(p, "precision") is not None and _min(p, main_recall) is not None
        ok = ok and _min(p, "precision") >= rule["min_precision"]
        ok = ok and _min(p, main_recall) >= rule["min_recall"]
        neighbours = [by.get((p["days"], p["score"], p["k"] + d)) for d in (-1, 1)]
        neighbours = [n for n in neighbours if n is not None]
        ok = ok and all(
            n["min_precision"] is not None and n["min_precision"] >= rule["neighbour_min_precision"]
            for n in neighbours
        )
        if ok:
            passing.append(p)
    return passing


def choose_prod(points: list[dict], main_recall: str, rule: dict = PROD_RULE) -> dict:
    """Smallest K (then the shorter horizon) passing :func:`rule_passing` among the rule's
    horizons and score; otherwise the K with the largest min F1 at the fallback horizon,
    marked as not meeting the ТЗ goal."""
    rows = [
        p
        for p in points
        if p["scope"] == rule["scope"] and p["score"] == rule["score"] and p["days"] in rule["days"]
    ]
    passing = rule_passing(rows, main_recall, rule)
    if passing:
        best = min(passing, key=lambda p: (p["k"], p["days"]))
        return {"goal_met": True, "point": _brief(best, main_recall), "passing": len(passing)}
    fallback = [p for p in rows if p["days"] == rule["fallback_days"]]
    best = max(fallback, key=lambda p: (_min(p, f"f1_{main_recall}") or -1.0, -p["k"]))
    return {
        "goal_met": False,
        "note": "цель ТЗ не выполнена: K с максимумом min F1 на 14 сут",
        "point": _brief(best, main_recall),
        "passing": 0,
    }


def rule_outside_its_grid(points: list[dict], main_recall: str, rule: dict = PROD_RULE):
    """For transparency: phase points of every horizon and score that pass the thresholds
    of the rule (not a selection; the rule itself allows only its horizons and score)."""
    rows = [p for p in points if p["scope"] == rule["scope"]]
    return [
        {
            "days": p["days"],
            "score": p["score"],
            "k": p["k"],
            "min_precision": p["min_precision"],
            f"min_{main_recall}": p[f"min_{main_recall}"],
        }
        for p in sorted(
            rule_passing(rows, main_recall, rule), key=lambda p: (p["days"], p["score"], p["k"])
        )
    ]


def choose_demo(points: list[dict], main_recall: str, rule: dict = DEMO_RULE) -> dict:
    """Per demo scope: the K with the largest min F1 (worse fold) at 14 days, static list."""
    out = {}
    for scope in rule["scopes"]:
        rows = [
            p
            for p in points
            if p["scope"] == scope and p["score"] == rule["score"] and p["days"] == rule["days"]
        ]
        best = max(rows, key=lambda p: (_min(p, f"f1_{main_recall}") or -1.0, -p["k"]))
        out[scope] = _brief(best, main_recall)
    return out


def _brief(p: dict, main_recall: str) -> dict:
    return {
        "scope": p["scope"],
        "days": p["days"],
        "score": p["score"],
        "k": p["k"],
        "main_recall": main_recall,
        "min_precision": p["min_precision"],
        **{f"min_{r}": p[f"min_{r}"] for r in rel.RECALLS},
        f"min_f1_{main_recall}": p[f"min_f1_{main_recall}"],
        "folds": [{m: f.get(m) for m in BRIEF_FOLD_FIELDS} for f in p["folds"]],
    }


BRIEF_FOLD_FIELDS = (
    "card_precision",
    "card_precision_lower_bound",
    *rel.RECALLS,
    "R_incident_fresh",
    "R_incident_chronic",
    "events",
    "incidents",
    "incidents_fresh",
    "incidents_chronic",
    "pairs_issued",
    "new_cards_per_day",
    "lead_hours_median_first",
)


# -- data --------------------------------------------------------------------------------


def _q(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _files(paths) -> str:
    return "[" + ", ".join(_q(p) for p in paths) + "]"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def connect() -> duckdb.DuckDBPyConnection:
    tmp = OUT / "tmp" / str(os.getpid())
    tmp.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET threads=2; SET memory_limit='4GB'; SET temp_directory={_q(tmp)}")
    return con


def blocked_days(con, end: str) -> pd.DataFrame:
    """Uncovered journal days (working coverage) and policy days before ``end``; the
    lookback check of the causal eligibility uses only days before each cutoff."""
    cov = con.execute(
        f"SELECT day, CAST(NULL AS VARCHAR) AS sensor_type_scope FROM read_parquet({_q(COVERAGE)}) "
        f"WHERE day < DATE {_q(end)} AND NOT working_covered"
    ).df()
    pol = con.execute(
        f"SELECT day, sensor_type_scope FROM read_parquet({_q(POLICY_DAYS)}) "
        f"WHERE day < DATE {_q(end)}"
    ).df()
    return pd.concat([cov, pol], ignore_index=True)


def load_events(con, end: str, root: Path = E5_LABELS) -> tuple[pd.DataFrame, pd.DataFrame]:
    base = root / "events" / TARGET
    cut = f"TIMESTAMP {_q(end)}"
    events = con.execute(
        f"SELECT * FROM read_parquet({_q(base / 'events.parquet')}) WHERE event_start < {cut}"
    ).df()
    members = con.execute(
        f"SELECT * FROM read_parquet({_q(base / 'event_members.parquet')}) WHERE start_at < {cut}"
    ).df()
    return events, members


def load_fold(con, days: int, year: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    start, end, _ = FOLDS[year]
    root = LABELS_7D if days == 7 else E5_LABELS
    months = [f"{year}-{m:02d}" for m in range(1, 13)]
    out = []
    for kind in ("cards", "links"):
        files = _files(root / f"{kind}/{TARGET}/{days * 24}h/{m}.parquet" for m in months)
        out.append(
            con.execute(
                f"SELECT * FROM read_parquet({files}) WHERE issued_at >= TIMESTAMP {_q(start)} "
                f"AND issued_at < TIMESTAMP {_q(end)} AND issued_at < TIMESTAMP {_q(DEV_END)}"
            ).df()
        )
    return out[0].reset_index(drop=True), out[1]


def load_cards(con, root: Path, days: int, start: str, end: str):
    """Cards and links of ``days`` issued in ``[start, end)`` from a label directory."""
    months = [
        p.strftime("%Y-%m")
        for p in pd.period_range(start, pd.Timestamp(end) - pd.Timedelta(days=1), freq="M")
    ]
    out = []
    for kind in ("cards", "links"):
        files = _files(root / f"{kind}/{TARGET}/{days * 24}h/{m}.parquet" for m in months)
        out.append(
            con.execute(
                f"SELECT * FROM read_parquet({files}) WHERE issued_at >= TIMESTAMP {_q(start)} "
                f"AND issued_at < TIMESTAMP {_q(end)}"
            ).df()
        )
    return out[0].reset_index(drop=True), out[1]


def pair_starts(con) -> pd.DataFrame:
    """First card of every pair in the development months (the pair is observed from it)."""
    files = _files(LABELS_7D / f"cards/{TARGET}/168h/{m}.parquet" for m in DEV_MONTHS)
    frame = con.execute(
        f"SELECT object_id, sensor_type, min(issued_at) AS first_card FROM read_parquet({files}) "
        f"WHERE issued_at < TIMESTAMP {_q(DEV_END)} GROUP BY 1, 2 ORDER BY 1, 2"
    ).df()
    frame["start"] = V6()["to_days"](frame["first_card"])
    return frame


_CACHE: dict = {}


def V6() -> dict:
    """v6 runner (Bayesian Poisson-gamma, history without 2021), loaded once."""
    if "v6" not in _CACHE:
        _CACHE["v6"] = runpy.run_path(str(HERE / "tune_sensor_failure_v6.py"), run_name="v6")
    return _CACHE["v6"]


def P2() -> dict:
    """v5 exploration 2 (90-day decay) and its frequency bins, loaded once."""
    if "p2" not in _CACHE:
        _CACHE["p2"] = runpy.run_path(
            str(HERE / "explore_sensor_failure_v5_part2.py"), run_name="p2"
        )
    return _CACHE["p2"]


def add_scores(cards, members, times, starts, prior: dict | None, days: int) -> pd.DataFrame:
    cards = cards.copy()
    cards["static_list"] = ev.static_list_scores(cards, members, RULE).to_numpy()
    cards["decay_90"] = P2()["decayed_counts"](cards, times, HALF_LIFE)
    if prior is not None:
        cards["bayes_pg"] = V6()["bayes_scores"](cards, times, starts, prior, days)
    return cards


def oracle(cards, links, events) -> np.ndarray:
    """Greedy oracle of exploration 5: positive cards first, more window events first."""
    q = set(events.loc[events["qualifies"].astype(bool), "event_id"])
    lk = links[links["candidate_member"].astype(bool) & links["event_id"].isin(q)]
    counts = lk.groupby(KEYS).event_id.nunique().rename("_n")
    n = cards[KEYS].merge(counts, left_on=KEYS, right_index=True, how="left")["_n"]
    return np.nan_to_num(cards["y_card"].to_numpy(dtype=float)) * (1 + n.fillna(0).to_numpy())


def incident_oracle(cards, links, events, heads: set) -> np.ndarray:
    """Oracle for R_incident: positive cards whose first window event starts an incident
    first, then the other positive cards. Under release a card catches only that event."""
    q = set(events.loc[events["qualifies"].astype(bool), "event_id"])
    lk = links[links["candidate_member"].astype(bool) & links["event_id"].isin(q)]
    lk = lk.sort_values([*KEYS, "event_start", "event_id"], kind="mergesort")
    first = lk.groupby(KEYS)["event_id"].first().rename("_e")
    e = cards[KEYS].merge(first, left_on=KEYS, right_index=True, how="left")["_e"]
    head = e.isin(heads).to_numpy()
    return np.nan_to_num(cards["y_card"].to_numpy(dtype=float)) * (1 + 2 * head)


# -- evaluation --------------------------------------------------------------------------


def point(sub, lk, events, mask, days, first, prev, inc, excluded, frames: bool = False):
    h = rel.honest_events(sub, lk, events, mask, days, "release", first)
    out = {**rel.card_summary(sub, mask), **rel.recall_summary(h, inc)}
    issued = pd.to_datetime(sub["issued_at"])
    active = issued[~excluded].dt.normalize().unique()
    per_day = pd.Series(mask.astype(int)).groupby(issued.dt.normalize().to_numpy()).sum()
    per_day = per_day[per_day.index.isin(active)]
    lead = h.loc[h["captured_first"], "lead_open"].to_numpy(dtype=float)
    known = sub["outcome"].isin(rel.EVALUABLE).to_numpy() & mask
    out["new_cards_per_day"] = float(per_day.mean()) if len(per_day) else None
    out["lead_hours_median_first"] = float(np.median(lead)) if len(lead) else None
    out["chronic_share_issued"] = float(prev[known].mean()) if known.any() else None
    return (out, h) if frames else out


def summarize(folds: list[dict]) -> dict:
    out = {"folds": folds}
    p = [f["card_precision"] for f in folds]
    out["min_precision"] = min(p) if all(v is not None for v in p) else None
    for r in rel.RECALLS:
        values = [f[r] for f in folds]
        out[f"min_{r}"] = min(values) if all(v is not None for v in values) else None
        f1s = [rel.f1(f["card_precision"], f[r]) for f in folds]
        out[f"min_f1_{r}"] = min(f1s) if all(v is not None for v in f1s) else None
    return out


def scope_stats(sub, lk, events, inc, first, days) -> dict:
    none = np.zeros(len(sub), dtype=bool)
    h = rel.honest_events(sub, lk, events, none, days, "release", first)
    s = rel.recall_summary(h, inc)
    known = sub["outcome"].isin(rel.EVALUABLE)
    return {
        "pairs": int(sub.loc[known, UNIT].drop_duplicates().shape[0]),
        "cards_known": int(known.sum()),
        "base_rate": float(sub.loc[known, "y_card"].mean()) if known.any() else None,
        "events": s["events"],
        "incidents": s["incidents"],
        "continuation_share": s["continuation_share"],
    }


def pair_start_after_midnight(members: pd.DataFrame, events: pd.DataFrame, scope: str) -> dict:
    """Events whose own pair member starts on a later calendar day than the event start
    (release at ``event_start`` precedes the pair's own failure across a cutoff)."""
    m = members[scope_mask(members, scope)]
    first = m.groupby(["event_id", *UNIT])["start_at"].min().rename("pair_start").reset_index()
    first = first.merge(events[["event_id", "event_start"]], on="event_id")
    later = (
        pd.to_datetime(first["pair_start"]).dt.normalize()
        > pd.to_datetime(first["event_start"]).dt.normalize()
    )
    return {"event_pairs": len(first), "pair_start_on_a_later_day": int(later.sum())}


def calibration_frames(sub, mask) -> pd.DataFrame:
    known = sub["outcome"].isin(rel.EVALUABLE).to_numpy() & mask
    return pd.DataFrame(
        {"count": sub.loc[known, "static_list"].to_numpy(), "y": sub.loc[known, "y_card"]}
    )


def calibration(build: pd.DataFrame, check: pd.DataFrame, min_n: int = CALIBRATION_MIN_N):
    """k of n per pair-frequency bin (static list count): bins of >= min_n issued cards
    on 2023, their Wilson intervals, and the share of 2024 bins inside them."""
    x = P2()["X"]
    edges = x["bin_edges"](build["count"].to_numpy(), min_n)
    fit = x["bin_table"](build["count"].to_numpy(), build["y"].to_numpy(dtype=float), edges)
    test = x["bin_table"](check["count"].to_numpy(), check["y"].to_numpy(dtype=float), edges)
    bins, hits, evaluated = [], 0, 0
    for f, c in zip(fit, test, strict=True):
        entry = {"counts": f["counts"], "2023": f, "2024": c, "evaluated": c["n"] >= min_n}
        if entry["evaluated"]:
            evaluated += 1
            entry["inside"] = bool(f["wilson_low"] <= c["wilson_point"] <= f["wilson_high"])
            hits += entry["inside"]
        bins.append(entry)
    all_cards = pd.concat([build, check], ignore_index=True)
    overall = wilson_interval(int(all_cards["y"].sum()), len(all_cards))
    return {
        "bins": bins,
        "evaluated_bins": evaluated,
        "inside": hits,
        "inside_share": hits / evaluated if evaluated else None,
        "overall_k_of_n": {"k": int(all_cards["y"].sum()), "n": len(all_cards), **overall},
    }


def evaluate_grid(result, parts, label, days, scores, events, inc, calib=None) -> None:
    """Points of one scope label: every score and K of its grid, both folds."""
    for score in scores:
        for k in budgets(label, days):
            per_fold = []
            for year, sub, lk, fs, elig, excl, _, pv in parts:
                mask = rel.select_rolling(sub, score, k=k, days=days, eligible=elig, first=fs)
                per_fold.append(point(sub, lk, events, mask, days, fs, pv, inc, excl))
                if calib is not None and score == "static_list":
                    calib[(days, k, year)] = calibration_frames(sub, mask)
            entry = summarize(per_fold)
            entry.update({"scope": label, "days": days, "score": score, "k": k})
            for f in per_fold:
                diff = abs(f["R_repeats"] - f["R_strict"])
                result["checks"]["R_repeats_minus_R_strict_max_abs"] = max(
                    result["checks"]["R_repeats_minus_R_strict_max_abs"], diff
                )
            result["points"].append(entry)


def scope_parts(cards, links, events, first, eligible, excluded, non_causal, prev, label, inc):
    """Cards of one scope with the two oracle scores and the per-fold masks."""
    m = scope_mask(cards, label) if label != FIRE_TARGET else np.ones(len(cards), dtype=bool)
    sub = cards[m].reset_index(drop=True)
    lk = links.merge(sub[KEYS], on=KEYS, how="inner")
    heads = set(inc.loc[inc["incident_first"], "event_id"])
    sub["ceiling"] = oracle(sub, lk, events)
    sub["ceiling_incident"] = incident_oracle(sub, lk, events, heads)
    fs = first[m].reset_index(drop=True)
    return sub, lk, fs, eligible[m], excluded[m], non_causal[m], prev[m]


def maintenance_inputs(members: pd.DataFrame, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """TO sessions and events of the target 'fire detector failure' (a + b1) from the
    journal ``[JOURNAL_FROM, end)`` with ``sensor_failure_maintenance`` (per object, as the
    DS-A reconciliation ``t7_reconcile_v2.py``). Connection losses of (a) are the smoke and
    heat members of the label events (all episodes)."""
    from infra_pulse_research import sensor_failure_maintenance as fm  # noqa: PLC0415
    from infra_pulse_research.modeling.fire_source import open_fire_source  # noqa: PLC0415

    con, _ = open_fire_source(ROOT)
    tmp = OUT / "tmp" / f"journal-{os.getpid()}"
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET threads=2; SET memory_limit='4GB'; SET temp_directory={_q(tmp)}")
    types = ", ".join(_q(t) for t in fm.ACTIVATION_STATES)
    last_month = (pd.Timestamp(end) - pd.Timedelta(days=1)).strftime("%Y-%m")
    con.execute(f"""
        CREATE TABLE raw AS SELECT channel_id, object_id, sensor_type, value_raw, alarm,
          TRY_CAST(event_ts_local_raw AS TIMESTAMP) AS ts, record_ordinal AS ord
        FROM working_events
        WHERE month BETWEEN {_q(JOURNAL_FROM[:7])} AND {_q(last_month)}
          AND sensor_type IN ({types}) AND value_numeric IS NULL
          AND NOT COALESCE(is_epoch_placeholder, false) AND object_id IS NOT NULL
          AND TRY_CAST(event_ts_local_raw AS TIMESTAMP) >= TIMESTAMP {_q(JOURNAL_FROM)}
          AND TRY_CAST(event_ts_local_raw AS TIMESTAMP) < TIMESTAMP {_q(end)}
    """)
    start = pd.to_datetime(members["start_at"])
    losses = members.loc[
        members["sensor_type"].isin(fm.TARGET_SENSOR_TYPES)
        & (start >= pd.Timestamp(JOURNAL_FROM))
        & (start < pd.Timestamp(end)),
        ["object_id", "sensor_type", "channel_id", "start_at"],
    ]
    objects = [
        o for (o,) in con.execute("SELECT DISTINCT object_id FROM raw ORDER BY 1").fetchall()
    ]
    sessions, fire_events = [], []
    for o in objects:
        runs = fm.state_runs(con.execute("SELECT * FROM raw WHERE object_id = ?", [o]).df())
        found = sm.maintenance_sessions(runs)
        sessions.append(found)
        fire_events.append(fm.fire_detector_events(losses[losses["object_id"] == o], runs, found))
    con.close()
    return (
        pd.concat(sessions, ignore_index=True),
        pd.concat(fire_events, ignore_index=True),
    )


def _by_year(values) -> dict:
    years = pd.to_datetime(pd.Series(values)).dt.year
    return {str(y): int(n) for y, n in years.value_counts().sort_index().items() if y >= 2023}


def masked_demos(result, con, events, members, blocked) -> None:
    """Demo scopes S1, S4, fire at 14 days with the TO mask of smoke and heat, and the demo
    target 'fire detector failure' (object × fire subsystem, a + b1); static list."""
    from infra_pulse_research import sensor_failure_maintenance as fm  # noqa: PLC0415

    sessions, fire_events = maintenance_inputs(members, DEV_END)
    masked = sm.masked_members(members, sessions)
    fire_members = members["sensor_type"].isin(fm.TARGET_SENSOR_TYPES).to_numpy()
    result["mask"] = {
        "definition_version": fm.DEFINITION_VERSION,
        "sessions_by_year": _by_year(sessions["session_start"]),
        "objects_with_sessions": int(sessions["object_id"].nunique()),
        "smoke_heat_members_by_year": _by_year(members.loc[fire_members, "start_at"]),
        "masked_members_by_year": _by_year(members.loc[masked, "start_at"]),
        "fire_target_events_by_year": {
            c: _by_year(fire_events.loc[fire_events["component"] == c, "start_at"])
            for c in ("a", "b1")
        },
    }
    days = DEMO_DAYS
    folds_m, folds_f = [], []
    for year, (_, _, period_end) in FOLDS.items():
        cards, links = load_fold(con, days, year)
        cards_m, links_m, members_m = sm.apply_mask(cards, links, events, members, masked)
        cards_m["static_list"] = ev.static_list_scores(cards_m, members_m, RULE).to_numpy()
        first = rel.first_event_starts(cards_m, links_m, events)
        eligible, excluded = rel.eligibility(
            cards_m, days, period_end=period_end, blocked_days=blocked
        )
        prev = ev.static_list_scores(cards_m, members_m, RULE, window_days=days).to_numpy() > 0
        folds_m.append((year, cards_m, links_m, first, eligible, excluded, eligible, prev))
        u_cards, u_links, u_events, u_members = sm.subsystem_labels(cards, fire_events, days)
        u_cards["static_list"] = ev.static_list_scores(u_cards, u_members, RULE).to_numpy()
        u_first = rel.first_event_starts(u_cards, u_links, u_events)
        u_elig, u_excl = rel.eligibility(u_cards, days, period_end=period_end, blocked_days=blocked)
        u_prev = ev.static_list_scores(u_cards, u_members, RULE, window_days=days).to_numpy() > 0
        folds_f.append(
            (year, u_cards, u_links, u_events, u_members, u_first, u_elig, u_excl, u_prev)
        )
    for scope in MASKED_SCOPES:
        label = f"{scope}{MASK_SUFFIX}"
        members_m = members[~masked]
        inc = rel.incidents(rel.event_pairs(members_m[scope_mask(members_m, scope)], events))
        parts = []
        for year, cards_m, links_m, first, elig, excl, nc, prev in folds_m:
            sub, lk, fs, e, x, n, pv = scope_parts(
                cards_m, links_m, events, first, elig, excl, nc, prev, label, inc
            )
            parts.append((year, sub, lk, fs, e, x, n, pv))
            result["scopes"].setdefault(label, {}).setdefault(f"{days}d", {})[str(year)] = (
                scope_stats(sub, lk, events, inc, fs, days)
            )
        evaluate_grid(result, parts, label, days, ("static_list", *CEILINGS), events, inc)
    u_events, u_members = folds_f[0][3], folds_f[0][4]
    inc = rel.incidents(rel.event_pairs(u_members, u_events))
    parts = []
    for year, u_cards, u_links, e_u, _, u_first, u_elig, u_excl, u_prev in folds_f:
        sub, lk, fs, e, x, n, pv = scope_parts(
            u_cards, u_links, e_u, u_first, u_elig, u_excl, u_elig, u_prev, FIRE_TARGET, inc
        )
        parts.append((year, sub, lk, fs, e, x, n, pv))
        result["scopes"].setdefault(FIRE_TARGET, {}).setdefault(f"{days}d", {})[str(year)] = (
            scope_stats(sub, lk, e_u, inc, fs, days)
        )
    evaluate_grid(result, parts, FIRE_TARGET, days, ("static_list", *CEILINGS), u_events, inc)


def run_dev() -> dict:
    started = perf_counter()
    con = connect()
    events, members = load_events(con, DEV_END)
    blocked = blocked_days(con, DEV_END)
    times = V6()["pair_event_times"](members, RULE)
    starts = pair_starts(con)
    priors = {}
    for year in FOLDS:
        histories = V6()["pair_histories"](times, starts, pd.Timestamp(f"{year}-01-01"))
        priors[year] = {**V6()["fit_bayes"](histories), "_half_life": HALF_LIFE}
    incidents = {
        s: rel.incidents(rel.event_pairs(members[scope_mask(members, s)], events)) for s in SCOPES
    }
    result = {
        "points": [],
        "scopes": {},
        "checks": {
            "first_event_start_mismatch_outside_excluded_cutoffs": 0,
            "R_repeats_minus_R_strict_max_abs": 0.0,
        },
        "blocked_days_since_2022": sorted(
            d
            for d in pd.to_datetime(blocked["day"]).dt.strftime("%Y-%m-%d").unique()
            if d >= "2022-01-01"
        ),
        "release_edge": {s: pair_start_after_midnight(members, events, s) for s in SCOPES},
    }
    calib_rows: dict = {}
    for days in HORIZONS:
        folds = []
        for year, (_, _, period_end) in FOLDS.items():
            cards, links = load_fold(con, days, year)
            cards = add_scores(cards, members, times, starts, priors[year], days)
            first = rel.first_event_starts(cards, links, events)
            eligible, excluded = rel.eligibility(
                cards, days, period_end=period_end, blocked_days=blocked
            )
            # Stored labels vs the links: equal except windows beyond 2025-01-01 (not loaded).
            stored = pd.to_datetime(cards["first_event_start"])
            differ = (stored != first) & (stored.notna() | first.notna())
            result["checks"]["first_event_start_mismatch_outside_excluded_cutoffs"] += int(
                (differ & ~excluded).sum()
            )
            non_causal, _ = rel.eligibility(cards, days, period_end=period_end, causal=False)
            prev = ev.static_list_scores(cards, members, RULE, window_days=days).to_numpy() > 0
            folds.append((year, cards, links, first, eligible, excluded, non_causal, prev))
        for scope in SCOPES:
            parts = []
            for year, cards, links, first, eligible, excluded, non_causal, prev in folds:
                sub, lk, fs, e, x, n, pv = scope_parts(
                    cards,
                    links,
                    events,
                    first,
                    eligible,
                    excluded,
                    non_causal,
                    prev,
                    scope,
                    incidents[scope],
                )
                parts.append((year, sub, lk, fs, e, x, n, pv))
                result["scopes"].setdefault(scope, {}).setdefault(f"{days}d", {})[str(year)] = (
                    scope_stats(sub, lk, events, incidents[scope], fs, days)
                )
            calib = calib_rows if scope == "phase" and days in (7, 14) else None
            evaluate_grid(
                result, parts, scope, days, (*SCORES, *CEILINGS), events, incidents[scope], calib
            )
            print(f"{days}d {scope} ({round(perf_counter() - started)} s)", flush=True)
        _CACHE[f"parts_{days}"] = folds
    masked_demos(result, con, events, members, blocked)
    print(f"masked demos and the fire target ({round(perf_counter() - started)} s)", flush=True)
    points = [p for p in result["points"] if p["score"] not in CEILINGS]
    result["selection"] = {}
    for main in MAIN_RECALLS:
        prod = choose_prod(points, main)
        demo = choose_demo(points, main)
        pt = prod["point"]
        build = calib_rows[(pt["days"], pt["k"], 2023)]
        check = calib_rows[(pt["days"], pt["k"], 2024)]
        prod["calibration_k_of_n"] = calibration(build, check)
        prod["non_causal_comparison"] = non_causal_point(events, incidents, pt)
        demo_mask = choose_demo(points, main, DEMO_MASK_RULE)
        for label, pt_m in demo_mask.items():
            before = next(
                p
                for p in points
                if p["scope"] == base_scope(label)
                and p["days"] == pt_m["days"]
                and p["score"] == pt_m["score"]
                and p["k"] == pt_m["k"]
            )
            pt_m["same_point_without_mask"] = _brief(before, main)
        fire = next(
            p
            for p in points
            if p["scope"] == FIRE_TARGET and p["score"] == "static_list" and p["k"] == FIRE_TARGET_K
        )
        result["selection"][main] = {
            "prod": prod,
            "demo_without_mask": demo,
            "demo_with_mask": demo_mask,
            "demo_fire_detector_failure": _brief(fire, main),
            "phase_points_passing_the_thresholds_at_any_horizon_or_score": rule_outside_its_grid(
                points, main
            ),
        }
    result["seconds"] = round(perf_counter() - started, 1)
    return result


def non_causal_point(events, incidents, pt: dict) -> list[dict]:
    """The chosen point with the earlier selection by known outcome (comparison only)."""
    out = []
    for year, cards, links, first, _, excluded, non_causal, prev in _CACHE[f"parts_{pt['days']}"]:
        m = scope_mask(cards, pt["scope"])
        sub = cards[m].reset_index(drop=True)
        lk = links.merge(sub[KEYS], on=KEYS, how="inner")
        fs = first[m].reset_index(drop=True)
        mask = rel.select_rolling(
            sub, pt["score"], k=pt["k"], days=pt["days"], eligible=non_causal[m], first=fs
        )
        res = point(
            sub, lk, events, mask, pt["days"], fs, prev[m], incidents[pt["scope"]], excluded[m]
        )
        out.append({"year": year, **{k: res[k] for k in ("card_precision", *rel.RECALLS)}})
    return out


# -- config and report -------------------------------------------------------------------


def provenance() -> dict:
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout

    return {
        "git_sha": git("rev-parse", "HEAD").strip(),
        "git_dirty": bool(git("status", "--porcelain").strip()),
        "labels": {
            "7d": str(LABELS_7D.relative_to(ROOT)),
            "14d, 21d, events": str(E5_LABELS.relative_to(ROOT)),
            "exploration5_labels_summary_sha256": _sha(E5_LABELS / "summary.json"),
        },
    }


POINT_FIELDS = {
    "key": "scope, days, score, K",
    "folds": "2023, 2024: card_precision, R_incident, R_strict, R_repeats, new_cards_per_day, "
    "R_incident_fresh, R_incident_chronic, pairs_issued",
}
_FOLD_FIELDS = (
    "card_precision",
    "R_incident",
    "R_strict",
    "R_repeats",
    "new_cards_per_day",
    "R_incident_fresh",
    "R_incident_chronic",
    "pairs_issued",
)


def compact(p: dict) -> list:
    """A point as ``[scope, days, score, K, [[fold 2023 fields], [fold 2024 fields]]]``."""

    def r(v):
        return None if v is None else round(float(v), 4)

    return [
        p["scope"],
        p["days"],
        p["score"],
        p["k"],
        [[r(f.get(m)) for m in _FOLD_FIELDS] for f in p["folds"]],
    ]


def write_json(path: Path, result: dict, one_line: str | None = None) -> None:
    """JSON with ``indent=1``; the list under ``one_line`` (if given) is written one
    element per line, so the tracked report stays small and diffable."""
    result = ev._py(result)
    rows = result.pop(one_line) if one_line else None
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if rows is not None:
        body = ",\n".join("  " + json.dumps(r, ensure_ascii=False) for r in rows)
        text = text[:-2] + f',\n "{one_line}": [\n{body}\n ]\n}}'
    text += "\n"
    if any(k in text for k in ('"object_id"', '"channel_id"', '"event_id"', '"node_id"')):
        raise ValueError("output must not contain identifiers")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _dev_min(p: dict) -> dict:
    return {m: p[f"min_{m}"] for m in ("precision", *rel.RECALLS)}


def draft_config(selection: dict, main_recall: str = "R_incident") -> dict:
    chosen = selection[main_recall]
    alternative = [m for m in MAIN_RECALLS if m != main_recall][0]
    prod = chosen["prod"]["point"]
    fire = chosen["demo_fire_detector_failure"]
    return {
        "version": "sensor-failure-final-v9",
        "status": "draft_not_frozen",
        "frozen_at": None,
        "holdout_use": "FIFTH use of the holdout 2025-07-01..2026-07-01 (after the freeze only)",
        "freeze_procedure": "Head DS confirms main_recall in writing; the status becomes "
        "'frozen_before_holdout_<date>_head_ds', the SHA-256 of this file and the time go to "
        "artifacts/next-2026-09-27/v9/freeze.log; only then the holdout labels are opened",
        "interpretation": "registered loss of connection ('Неисправен'); for the phase sensor "
        "it means loss of input power; not a confirmed breakdown; alarm is not a failure",
        "target": TARGET,
        "episodes": "all episodes (no duration threshold; user decision 27.09.2026)",
        "card": "object_id x sensor_type; issued daily at 00:00 local; point in time",
        "policy": "rolling list with release: a card leaves the list at the start of the first "
        "event of its pair (sensor_failure_release.release_at); causal selection "
        "(sensor_failure_release.eligibility with the covered lookback)",
        "recall": {
            "main_recall": main_recall,
            "alternative": alternative,
            "definitions": {
                "R_repeats": "event caught if a card of its pair is open at its start (reference)",
                "R_strict": "event caught only if it is the first event of an open card",
                "R_incident": "events of a pair < 24 h apart (chained) form one incident; caught "
                "if a card is open at the start of its first event",
            },
            "precision": "per issued card with a known outcome: an event in its window; the "
            "lower bound counts unknown outcomes as misses",
        },
        "selection_rule": {
            "basis": "development folds 2023 and 2024, worse fold; exploration on the same "
            "folds (multiplicity: see the independent audit, item 7)",
            "prod": {k: list(v) if isinstance(v, tuple) else v for k, v in PROD_RULE.items()},
            "demo": {
                **{k: list(v) if isinstance(v, tuple) else v for k, v in DEMO_RULE.items()},
                "k": "largest min F1 (main recall) on dev WITH the TO mask",
            },
            "demo_fire_detector_failure": "K = 5 fixed by Head DS",
        },
        "configurations": {
            "prod_phase": {
                "scope": "phase",
                "sensor_types": [PHASE],
                "labels": "connection loss, all episodes; the TO mask does not apply",
                "days": prod["days"],
                "k": prod["k"],
                "score": "static_list_365",
                "goal_met_on_dev": chosen["prod"]["goal_met"],
                "dev_min": _dev_min(prod),
                "also": "k-of-n calibration by pair-frequency bins built on 2023/2024",
            },
            **{
                f"demo_{base_scope(label)}": {
                    "scope": base_scope(label),
                    "labels": "connection loss, all episodes, WITH the TO-session mask of smoke "
                    f"and heat ({MASK_VERSION}, ±30 min)",
                    "days": p["days"],
                    "k": p["k"],
                    "score": "static_list_365",
                    "dev_min": _dev_min(p),
                    "reported_beside": "the same point without the mask (before/after)",
                    "dev_min_without_mask": _dev_min(p["same_point_without_mask"]),
                }
                for label, p in chosen["demo_with_mask"].items()
            },
            "demo_fire_detector_failure": {
                "unit": "object x fire subsystem (smoke + heat together)",
                "labels": "DS-A target a + b1 (sensor_failure_maintenance.fire_detector_events): "
                "connection-loss starts outside the TO mask + series of isolated unconfirmed "
                "self-resets outside the mask; events of an object at the same moment are one",
                "days": fire["days"],
                "k": fire["k"],
                "score": "static_list_365",
                "dev_min": _dev_min(fire),
                "expectation_ds_a_dev": "P 0.57-0.68, R_incident 0.48-0.53 (honesty check, "
                "not a claim of the ТЗ goal)",
            },
            "demo_reporting": "R_incident of fresh and chronic incidents (no event of the "
            "pair in the 14 days before the incident) and distinct pairs issued a year",
        },
        "alternative_if_main_recall_is": {
            alternative: {
                "prod_phase": {
                    "days": selection[alternative]["prod"]["point"]["days"],
                    "k": selection[alternative]["prod"]["point"]["k"],
                    "goal_met_on_dev": selection[alternative]["prod"]["goal_met"],
                },
                **{
                    f"demo_{base_scope(s)}": {"days": p["days"], "k": p["k"]}
                    for s, p in selection[alternative]["demo_with_mask"].items()
                },
            }
        },
        "open_for_head_ds": [
            "prod rule: no K at 7/14 days passes min P >= 0.74 with stable neighbours; the "
            "fallback (max min F1 at 14 days) is recorded; alternatives in FINAL_V9.md",
        ],
        "holdout_plan": {
            "period": ["2025-07-01", "2026-07-01"],
            "labels": "artifacts/sensor-failure-v1/model_labels_v5_holdout (connection_loss_all)",
            "runs": "one run of the frozen configurations; on a crash one repeat after a code "
            "fix, marked in the report",
            "bootstrap": "blocked by ISO week and, separately, by object; 400 replicates, seed 17; "
            "cards by the week of issue, events and incidents by the week of the (first) event "
            "start; 95% intervals, lower bounds reported",
            "criterion_tz": "prod_phase: lower bound of P >= 0.70 and of main_recall >= 0.50 "
            "(week blocks); demo scopes: reported only",
            "also": "the three recalls side by side, k-of-n calibration of the phase point "
            "(bins of 2023/2024), new cards a day, lead",
            "mask_and_fire_target": "TO sessions and the fire target a + b1 with "
            f"sensor_failure_maintenance ({MASK_VERSION}) from the journal of the holdout "
            "period and the 365 days before it (history of the static list); the phase is not "
            "masked; the demo points are also reported without the mask",
            "folds_beside": "the same code on the folds 2023/2024 (dev values of this config)",
        },
        "mlflow": {
            "tracking_uri": "http://127.0.0.1:5059",
            "experiment": "sensor-failure-final-v9",
        },
    }


# -- phase 2: the frozen plan, the freeze and the single holdout run -----------------------

HOLDOUT_LABELS = SF / "model_labels_v5_holdout"
FREEZE_LOG = OUT / "freeze.log"
DRY_RUN = OUT / "dry_run.json"
HOLDOUT_OUT = OUT / "holdout_run.json"
DEV_POINTS = OUT / "dev_points.json"
MODELING = ROOT / "data-science/src/infra_pulse_research/modeling"
FROZEN_CODE = {
    "final_sensor_failure_v9.py": HERE / "final_sensor_failure_v9.py",
    "sensor_failure_release.py": MODELING / "sensor_failure_release.py",
    "sensor_failure_service_mask.py": MODELING / "sensor_failure_service_mask.py",
    "sensor_failure_maintenance.py": (
        ROOT / "data-science/src/infra_pulse_research/sensor_failure_maintenance.py"
    ),
}
PERIODS = {
    "2023": {
        "labels": E5_LABELS,
        "start": "2023-01-01",
        "end": "2024-01-01",
        "period_end": None,
        "data_end": DEV_END,
    },
    "2024": {
        "labels": E5_LABELS,
        "start": "2024-01-01",
        "end": "2025-01-01",
        "period_end": "2025-01-01",
        "data_end": DEV_END,
    },
    "holdout": {
        "labels": HOLDOUT_LABELS,
        "start": "2025-07-01",
        "end": "2026-07-01",
        "period_end": "2026-07-01",
        "data_end": "2026-07-01",
    },
}
HOLDOUT_USE = 5
REPLICATES, SEED = 400, 17
BLOCK_SCHEMES = ("iso_week", "object_id")
TZ_GOAL = {"card_precision": 0.70, "R_incident": 0.50}
BOOT_METRICS = (
    "card_precision",
    "R_incident",
    "R_strict",
    "R_repeats",
    "R_incident_fresh",
    "R_incident_chronic",
)
# Head DS decisions of 27.09.2026 (written confirmation of the freeze): name, role, scope
# label, K, note. All: 14 days, release, static list 365 days, causal selection.
FROZEN_PLAN = (
    (
        "prod_phase",
        "primary",
        "phase",
        10,
        "основная конфигурация продукта: запасной вариант правила (max min F1 на 14 сут); "
        "цель ТЗ на dev не выполнена по правилу с запасом; выбрана до прогона на отложенном",
    ),
    (
        "prod_phase_k7",
        "secondary",
        "phase",
        7,
        "объявлена заранее, выбрана после просмотра dev: цель без запаса, min P 0,719; "
        "только для отчёта, заявка по ТЗ — только по основной",
    ),
    ("demo_S1_mask_k13", "demo", "S1+mask", 13, "описательная точка"),
    ("demo_S1_k13", "demo_without_mask", "S1", 13, "та же точка без маски"),
    ("demo_S4_mask_k14", "demo", "S4+mask", 14, "описательная точка"),
    ("demo_S4_k14", "demo_without_mask", "S4", 14, "та же точка без маски"),
    ("demo_S4_mask_k5", "demo", "S4+mask", 5, "описательная точка"),
    ("demo_S4_k5", "demo_without_mask", "S4", 5, "та же точка без маски"),
    ("demo_fire_mask_k6", "demo", "fire+mask", 6, "описательная точка"),
    ("demo_fire_k6", "demo_without_mask", "fire", 6, "та же точка без маски"),
    ("demo_fire_mask_k5", "demo", "fire+mask", 5, "описательная точка"),
    ("demo_fire_k5", "demo_without_mask", "fire", 5, "та же точка без маски"),
    (
        "demo_fire_detector_failure_k5",
        "demo",
        FIRE_TARGET,
        5,
        "цель DS-A a + b1, объект × пожарная подсистема; проверка честности",
    ),
)
POINT_KEYS = (
    "cards_issued",
    "pairs_issued",
    "cards_known",
    "cards_unknown_outcome",
    "card_precision",
    "card_precision_lower_bound",
    "events",
    "incidents",
    "continuation_share",
    "R_repeats",
    "R_strict",
    "R_incident",
    "incidents_fresh",
    "R_incident_fresh",
    "incidents_chronic",
    "R_incident_chronic",
    "new_cards_per_day",
    "lead_hours_median_first",
    "chronic_share_issued",
)


def plan_configurations(dev_points: list[dict]) -> dict:
    """The frozen configurations with their dev values (from the phase-1 points)."""
    by = {(p["scope"], p["days"], p["score"], p["k"]): p for p in dev_points}
    out = {}
    for name, role, label, k, note in FROZEN_PLAN:
        dev = by[(label, DEMO_DAYS, "static_list", k)]
        base = base_scope(label)
        out[name] = {
            "role": role,
            "scope_label": label,
            "scope": base,
            "unit": "object x fire subsystem" if label == FIRE_TARGET else "object x sensor type",
            "labels": (
                "DS-A target a + b1 (outside the TO mask)"
                if label == FIRE_TARGET
                else "connection loss, all episodes"
                + (f", TO mask of smoke and heat ({MASK_VERSION})" if MASK_SUFFIX in label else "")
            ),
            "days": DEMO_DAYS,
            "k": k,
            "score": "static_list_365",
            "policy": "release",
            "note": note,
            "dev": {
                year: {m: f.get(m) for m in POINT_KEYS}
                for year, f in zip(("2023", "2024"), dev["folds"], strict=True)
            },
        }
    return out


def code_hashes() -> dict:
    return {name: _sha(path) for name, path in FROZEN_CODE.items()}


def read_freeze_log(path: Path = FREEZE_LOG) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def check_freeze(entries: list[dict], config_sha: str, code: dict, repeat: bool) -> str:
    """Why the holdout may not be read (empty string: it may). The last entry with hashes
    must match the frozen config and code; one run, one repeat after a logged fix."""
    hashed = [e for e in entries if "sha256" in e]
    if not hashed or hashed[0]["event"] != "freeze":
        return "no freeze entry"
    last = hashed[-1]["sha256"]
    if last.get("config") != config_sha:
        return "config differs from the freeze log"
    changed = [n for n, h in code.items() if last.get(n) != h]
    if changed:
        return f"code differs from the freeze log: {changed}"
    started = [e for e in entries if e["event"] == "holdout_started"]
    finished = [e for e in entries if e["event"] == "holdout_finished"]
    if finished:
        return "the holdout run is finished; no further run"
    if not repeat and started:
        return "a run was started; only --repeat after a logged code fix"
    if repeat:
        if len(started) != 1:
            return "a repeat needs exactly one started run"
        if hashed[-1]["event"] != "repeat_after_fix":
            return "a repeat needs a 'repeat_after_fix' entry with the new hashes"
    return ""


def log_event(event: str, **fields) -> dict:
    entry = {
        "time": pd.Timestamp.now(tz="Europe/Moscow").isoformat(timespec="seconds"),
        "event": event,
    }
    entry.update(fields)
    FREEZE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with FREEZE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def period_context(con, name: str) -> dict:
    """Events, members, blocked days, the TO mask and the fire-target events of a period's
    data (cached per label directory and data end)."""
    spec = PERIODS[name]
    key = ("ctx", str(spec["labels"]), spec["data_end"])
    if key not in _CACHE:
        events, members = load_events(con, spec["data_end"], spec["labels"])
        sessions, fire_events = maintenance_inputs(members, spec["data_end"])
        masked = sm.masked_members(members, sessions)
        kept = members[~masked]
        incidents = {
            s: rel.incidents(rel.event_pairs(members[scope_mask(members, s)], events))
            for s in SCOPES
        }
        incidents |= {
            f"{s}{MASK_SUFFIX}": rel.incidents(rel.event_pairs(kept[scope_mask(kept, s)], events))
            for s in MASKED_SCOPES
        }
        _CACHE[key] = {
            "events": events,
            "members": members,
            "blocked": blocked_days(con, spec["data_end"]),
            "sessions": sessions,
            "fire_events": fire_events,
            "masked": masked,
            "incidents": incidents,
        }
    return _CACHE[key]


def period_tables(con, name: str) -> dict:
    """The three label variants of a period at 14 days: plain, TO-masked, fire target."""
    spec = PERIODS[name]
    ctx = period_context(con, name)
    events, members, blocked = ctx["events"], ctx["members"], ctx["blocked"]
    days, period_end = DEMO_DAYS, spec["period_end"]
    cards, links = load_cards(con, spec["labels"], days, spec["start"], spec["end"])

    def variant(c, lk, ev_, mem):
        c = c.copy()
        c["static_list"] = ev.static_list_scores(c, mem, RULE).to_numpy()
        first = rel.first_event_starts(c, lk, ev_)
        eligible, excluded = rel.eligibility(c, days, period_end=period_end, blocked_days=blocked)
        prev = ev.static_list_scores(c, mem, RULE, window_days=days).to_numpy() > 0
        return {
            "cards": c,
            "links": lk,
            "events": ev_,
            "first": first,
            "eligible": eligible,
            "excluded": excluded,
            "prev": prev,
        }

    cards_m, links_m, members_m = sm.apply_mask(cards, links, events, members, ctx["masked"])
    u_cards, u_links, u_events, u_members = sm.subsystem_labels(cards, ctx["fire_events"], days)
    incidents = dict(ctx["incidents"])
    incidents[FIRE_TARGET] = rel.incidents(rel.event_pairs(u_members, u_events))
    return {
        "plain": variant(cards, links, events, members),
        "mask": variant(cards_m, links_m, events, members_m),
        "fire": variant(u_cards, u_links, u_events, u_members),
        "incidents": incidents,
        "counts": {
            "cards": len(cards),
            "sessions": int(
                (
                    (
                        pd.to_datetime(ctx["sessions"]["session_start"])
                        >= pd.Timestamp(spec["start"])
                    )
                    & (pd.to_datetime(ctx["sessions"]["session_start"]) < pd.Timestamp(spec["end"]))
                ).sum()
            ),
        },
    }


def frozen_point(tables: dict, cfg: dict) -> dict:
    """One frozen configuration on one period: the point, the rows of the bootstrap and the
    issued known cards for the k-of-n calibration."""
    label, k = cfg["scope_label"], int(cfg["k"])
    kind = "fire" if label == FIRE_TARGET else ("mask" if MASK_SUFFIX in label else "plain")
    v, inc = tables[kind], tables["incidents"][label]
    sub, lk, fs, elig, excl, _, prev = scope_parts(
        v["cards"],
        v["links"],
        v["events"],
        v["first"],
        v["eligible"],
        v["excluded"],
        v["eligible"],
        v["prev"],
        label,
        inc,
    )
    mask = rel.select_rolling(sub, "static_list", k=k, days=DEMO_DAYS, eligible=elig, first=fs)
    res, h = point(sub, lk, v["events"], mask, DEMO_DAYS, fs, prev, inc, excl, frames=True)
    known = mask & sub["outcome"].isin(rel.EVALUABLE).to_numpy()
    heads = h.merge(
        inc[["event_id", "incident_first", "incident_fresh"]], on="event_id", how="left"
    )
    heads = heads[heads["incident_first"].astype(bool)]
    frames = {
        "cards": pd.DataFrame(
            {
                "at": pd.to_datetime(sub.loc[known, "issued_at"]).to_numpy(),
                "object_id": sub.loc[known, "object_id"].to_numpy(),
                "y": sub.loc[known, "y_card"].to_numpy(dtype=float),
            }
        ),
        "events": pd.DataFrame(
            {
                "at": pd.to_datetime(h["event_start"]).to_numpy(),
                "object_id": h["object_id"].to_numpy(),
                "strict": h["captured_first"].to_numpy(dtype=float),
                "repeats": h["captured_open"].to_numpy(dtype=float),
            }
        ),
        "heads": pd.DataFrame(
            {
                "at": pd.to_datetime(heads["event_start"]).to_numpy(),
                "object_id": heads["object_id"].to_numpy(),
                "caught": heads["captured_open"].to_numpy(dtype=float),
                "fresh": heads["incident_fresh"].astype(bool).to_numpy(),
            }
        ),
    }
    return {
        "point": {m: res.get(m) for m in POINT_KEYS},
        "frames": frames,
        "calibration": calibration_frames(sub, mask),
    }


def block_bootstrap(frames: dict, scheme: str, replicates: int = REPLICATES, seed: int = SEED):
    """Paired block bootstrap of every configuration: ISO weeks (cards by issue, events and
    incidents by their start) or objects; the same draws for all configurations."""
    keys = []
    for f in frames.values():
        for part in ("cards", "events", "heads"):
            keys.append(ev._block_keys(f[part]["at"], f[part]["object_id"], scheme))
    codes, uniques = pd.factorize(pd.concat(keys, ignore_index=True))
    n = len(uniques)
    rng = np.random.default_rng(seed + (1 if scheme == "object_id" else 0))
    draws = rng.integers(0, n, size=(replicates, n))
    weights = np.stack([np.bincount(d, minlength=n) for d in draws]).astype(float)
    ones = np.ones((1, n))
    pos, out = 0, {}
    for name, f in frames.items():
        c = {}
        for part in ("cards", "events", "heads"):
            c[part] = codes[pos : pos + len(f[part])]
            pos += len(f[part])
        fresh = f["heads"]["fresh"].to_numpy()
        spec = {
            "card_precision": ("cards", f["cards"]["y"].to_numpy(), None),
            "R_incident": ("heads", f["heads"]["caught"].to_numpy(), None),
            "R_strict": ("events", f["events"]["strict"].to_numpy(), None),
            "R_repeats": ("events", f["events"]["repeats"].to_numpy(), None),
            "R_incident_fresh": ("heads", f["heads"]["caught"].to_numpy(), fresh),
            "R_incident_chronic": ("heads", f["heads"]["caught"].to_numpy(), ~fresh),
        }
        res = {}
        for metric, (part, values, keep) in spec.items():
            idx = c[part] if keep is None else c[part][keep]
            vals = values if keep is None else values[keep]
            with np.errstate(invalid="ignore", divide="ignore"):
                pt = (ones[:, idx] @ vals) / ones[:, idx].sum(axis=1)
                reps = (weights[:, idx] @ vals) / weights[:, idx].sum(axis=1)
            res[metric] = _interval(float(pt[0]) if len(vals) else None, reps)
        out[name] = res
    return {"scheme": scheme, "blocks": n, "replicates": replicates, "seed": seed, "metrics": out}


def calibration_vs_dev(build: pd.DataFrame, check: pd.DataFrame, min_n: int = CALIBRATION_MIN_N):
    """k of n by pair-frequency bins built on dev 2023+2024 (>= min_n issued cards per bin);
    a holdout bin with >= min_n cards is 'inside' if its share lies in the dev Wilson
    interval of the bin."""
    x = P2()["X"]
    edges = x["bin_edges"](build["count"].to_numpy(), min_n)
    fit = x["bin_table"](build["count"].to_numpy(), build["y"].to_numpy(dtype=float), edges)
    test = x["bin_table"](check["count"].to_numpy(), check["y"].to_numpy(dtype=float), edges)
    bins, inside, evaluated = [], 0, 0
    for f, c in zip(fit, test, strict=True):
        entry = {"counts": f["counts"], "dev": f, "holdout": c, "evaluated": c["n"] >= min_n}
        if entry["evaluated"]:
            evaluated += 1
            entry["inside"] = bool(f["wilson_low"] <= c["wilson_point"] <= f["wilson_high"])
            inside += entry["inside"]
        bins.append(entry)

    def overall(frame):
        k, n = int(frame["y"].sum()), len(frame)
        return {"k": k, "n": n, **wilson_interval(k, n)}

    return {
        "bins": bins,
        "evaluated_bins": evaluated,
        "inside": inside,
        "inside_share": inside / evaluated if evaluated else None,
        "dev_overall": overall(build),
        "holdout_overall": overall(check),
    }


def compare_to_dev(result: dict, dev_points: list[dict]) -> dict:
    """Frozen configurations recomputed on the folds vs the phase-1 points: exact equality."""
    by = {(p["scope"], p["days"], p["score"], p["k"]): p for p in dev_points}
    mismatches, compared = [], 0
    for period, configs in result.items():
        fold = {"2023": 0, "2024": 1}[period]
        for name, got in configs.items():
            ref = by[(got["scope_label"], DEMO_DAYS, "static_list", got["k"])]["folds"][fold]
            for m in POINT_KEYS:
                compared += 1
                if ref.get(m) != got["point"].get(m):
                    mismatches.append(
                        {
                            "period": period,
                            "config": name,
                            "metric": m,
                            "dev": ref.get(m),
                            "recomputed": got["point"].get(m),
                        }
                    )
    return {"compared": compared, "mismatches": mismatches, "all_equal": not mismatches}


def run_periods(con, configs: dict, periods: list[str]) -> dict:
    out = {}
    for name in periods:
        tables = period_tables(con, name)
        out[name] = {}
        for cname, cfg in configs.items():
            fp = frozen_point(tables, cfg)
            out[name][cname] = {"scope_label": cfg["scope_label"], "k": cfg["k"], **fp}
        out[name]["_counts"] = tables["counts"]
        print(f"{name} done", flush=True)
    return out


def strip(result: dict) -> dict:
    """Points only (no frames), for comparison and reports."""
    return {
        period: {
            n: {"scope_label": v["scope_label"], "k": v["k"], "point": v["point"]}
            for n, v in configs.items()
            if not n.startswith("_")
        }
        for period, configs in result.items()
    }


def verdict(point: dict, week: dict) -> dict:
    """ТЗ goal of the primary configuration by the point and by the lower bounds (weeks)."""
    p_low = week["card_precision"]["low"]
    r_low = week["R_incident"]["low"]
    return {
        "goal": TZ_GOAL,
        "point": {
            "card_precision": point["card_precision"],
            "R_incident": point["R_incident"],
            "pass": bool(
                point["card_precision"] >= TZ_GOAL["card_precision"]
                and point["R_incident"] >= TZ_GOAL["R_incident"]
            ),
        },
        "lower_bound_weeks": {
            "card_precision": p_low,
            "R_incident": r_low,
            "pass": bool(
                p_low is not None
                and r_low is not None
                and p_low >= TZ_GOAL["card_precision"]
                and r_low >= TZ_GOAL["R_incident"]
            ),
        },
    }


def _close(a, b, tol: float = 1e-12) -> bool:
    """Equal up to floating-point summation order (a mean vs a weighted sum)."""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol


def log_repeat_fix() -> dict:
    """After a crash of the started run and a code fix: the new hashes (config unchanged)."""
    entries = read_freeze_log()
    started = [e for e in entries if e["event"] == "holdout_started"]
    finished = [e for e in entries if e["event"] == "holdout_finished"]
    repeats = [e for e in entries if e["event"] == "repeat_after_fix"]
    if len(started) != 1 or finished or repeats:
        raise SystemExit("a repeat is allowed once, only after one started, unfinished run")
    freeze_entry = next(e for e in entries if e["event"] == "freeze")
    if freeze_entry["sha256"]["config"] != _sha(CONFIG):
        raise SystemExit("the frozen config must not change")
    return log_event("repeat_after_fix", sha256={"config": _sha(CONFIG), **code_hashes()})


def dry_run() -> dict:
    """Pre-freeze check: the holdout code with 2023/2024 in the role of the holdout must
    reproduce the phase-1 dev numbers of every frozen configuration exactly."""
    started = perf_counter()
    dev_points = json.loads(DEV_POINTS.read_text(encoding="utf-8"))["points"]
    configs = plan_configurations(dev_points)
    con = connect()
    result = run_periods(con, configs, ["2023", "2024"])
    check = compare_to_dev(strip(result), dev_points)
    frames = {n: v["frames"] for n, v in result["2024"].items() if not n.startswith("_")}
    boot = {s: block_bootstrap(frames, s) for s in BLOCK_SCHEMES}
    point_equal = all(
        _close(boot[sch]["metrics"][n][m]["point"], result["2024"][n]["point"][m])
        for sch in BLOCK_SCHEMES
        for n in frames
        for m in ("card_precision", "R_incident", "R_strict", "R_repeats")
    )
    out = {
        "stage": "dry run before the freeze: 2023/2024 in the role of the holdout",
        "check": check,
        "bootstrap_point_equals_point": point_equal,
        "code_sha256": code_hashes(),
        "seconds": round(perf_counter() - started, 1),
        **provenance(),
    }
    write_json(DRY_RUN, out)
    return out


def freeze() -> dict:
    """Write the frozen config and the freeze log (after a clean dry run of this code)."""
    dry = json.loads(DRY_RUN.read_text(encoding="utf-8")) if DRY_RUN.exists() else {}
    if not (dry.get("check", {}).get("all_equal") and dry.get("bootstrap_point_equals_point")):
        raise SystemExit("no clean dry run")
    if dry["code_sha256"] != code_hashes():
        raise SystemExit("the code changed after the dry run")
    existing = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    if str(existing.get("status", "")).startswith("frozen"):
        raise SystemExit("the v9 config is already frozen")
    dev_points = json.loads(DEV_POINTS.read_text(encoding="utf-8"))["points"]
    config = frozen_config(existing, plan_configurations(dev_points))
    write_json(CONFIG, config)
    return log_event(
        "freeze",
        holdout_use=HOLDOUT_USE,
        dry_run={"compared": dry["check"]["compared"], "all_equal": True},
        sha256={"config": _sha(CONFIG), **code_hashes()},
    )


def frozen_config(draft: dict, configurations: dict) -> dict:
    now = pd.Timestamp.now(tz="Europe/Moscow").isoformat(timespec="seconds")
    keep = ("version", "interpretation", "target", "episodes", "card", "policy", "recall")
    config = {k: draft[k] for k in keep if k in draft}
    config["recall"] = {**config.get("recall", {}), "main_recall": "R_incident"}
    config["recall"]["fresh_incident"] = (
        "no event of the pair in the 14 days before the start of the incident "
        "(Head DS decision 27.09.2026)"
    )
    return {
        **config,
        "status": "frozen",
        "frozen_at": now,
        "frozen_by": "Head DS written confirmation relayed 27.09.2026; executed by DS",
        "holdout_use": HOLDOUT_USE,
        "holdout_use_text": "FIFTH use of the holdout 2025-07-01..2026-07-01",
        "frozen_before": "any read of the labels, cards, links, events or journal rows of the "
        "holdout period; the dry run used 2023/2024 only",
        "selection_basis": "prod: the fallback of the v9 rule on dev (max min F1 at 14 days); "
        "the rule is not changed afterwards. Secondary K = 7: declared before the run, "
        "chosen after seeing dev, report only. Demo points are descriptive, no selection. "
        "21 days, 90-day decay and Bayes pass the rule on dev outside its limits and are "
        "not part of the freeze",
        "configurations": configurations,
        "criterion_tz": {
            "configuration": "prod_phase",
            "goal": TZ_GOAL,
            "decision": "by the lower bounds of the 95% week-block bootstrap intervals; the "
            "point estimate reported beside it; the claim for the ТЗ only by prod_phase",
        },
        "holdout_plan": {
            "periods": {
                k: {
                    kk: (str(vv) if isinstance(vv, Path) else vv)
                    for kk, vv in v.items()
                    if kk != "labels"
                }
                for k, v in PERIODS.items()
            },
            "labels": "artifacts/sensor-failure-v1/model_labels_v5_holdout (connection_loss_all, "
            "336h); history before the period from the same labels",
            "mask": f"{MASK_VERSION}: TO sessions and the fire target from the journal "
            f"{JOURNAL_FROM}..2026-06-30; phase not masked",
            "bootstrap": f"{REPLICATES} replicates, seed {SEED} (objects: seed + 1), blocks ISO "
            "week and object; cards by issue, events and incidents by the (first) event start",
            "calibration": "k of n of prod_phase: bins of >= 30 issued cards on dev 2023+2024, "
            "holdout share inside the dev Wilson interval",
            "runs": "one run; on a crash one repeat after a code fix with new hashes in "
            "freeze.log, marked in the report",
            "folds_beside": "the same code on 2023/2024 must reproduce the dev points before "
            "the holdout is read",
        },
    }


def run_holdout(repeat: bool) -> dict:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    if config.get("status") != "frozen":
        raise SystemExit("the v9 config is not frozen")
    reason = check_freeze(read_freeze_log(), _sha(CONFIG), code_hashes(), repeat)
    if reason:
        raise SystemExit(f"holdout refused: {reason}")
    started = perf_counter()
    configs = config["configurations"]
    dev_points = json.loads(DEV_POINTS.read_text(encoding="utf-8"))["points"]
    con = connect()
    dev = run_periods(con, configs, ["2023", "2024"])
    check = compare_to_dev(strip(dev), dev_points)
    if not check["all_equal"]:
        raise SystemExit(f"folds differ from dev before the holdout: {check['mismatches'][:3]}")
    log_event("holdout_started", repeat=repeat)
    hold = run_periods(con, configs, ["holdout"])["holdout"]
    frames = {n: v["frames"] for n, v in hold.items() if not n.startswith("_")}
    boot = {s: block_bootstrap(frames, s) for s in BLOCK_SCHEMES}
    build = pd.concat(
        [dev[p]["prod_phase"]["calibration"] for p in ("2023", "2024")], ignore_index=True
    )
    ctx = period_context(con, "holdout")
    result = {
        "stage": "v9 phase 2: the single run of the frozen configurations on the holdout",
        "holdout_use": HOLDOUT_USE,
        "repeat_after_fix": repeat,
        "frozen_at": config["frozen_at"],
        "config_sha256": _sha(CONFIG),
        "code_sha256": code_hashes(),
        "folds_reproduce_dev": check,
        "configurations": {
            n: {
                "role": c["role"],
                "scope_label": c["scope_label"],
                "k": c["k"],
                "holdout": hold[n]["point"],
                "dev": {p: dev[p][n]["point"] for p in ("2023", "2024")},
                "ci_weeks": boot["iso_week"]["metrics"][n],
                "ci_objects": boot["object_id"]["metrics"][n],
            }
            for n, c in configs.items()
        },
        # Scheme names are not written as keys: "object_id" would trip the identifier guard.
        "blocks": {"weeks": boot["iso_week"]["blocks"], "objects": boot["object_id"]["blocks"]},
        "criterion_tz_prod_phase": verdict(
            hold["prod_phase"]["point"], boot["iso_week"]["metrics"]["prod_phase"]
        ),
        "criterion_tz_prod_phase_k7_report_only": verdict(
            hold["prod_phase_k7"]["point"], boot["iso_week"]["metrics"]["prod_phase_k7"]
        ),
        "calibration_k_of_n_prod_phase": calibration_vs_dev(
            build, hold["prod_phase"]["calibration"]
        ),
        "holdout_counts": {
            "cards_14d": hold["_counts"]["cards"],
            "sessions": hold["_counts"]["sessions"],
            "fire_target_events": {
                c: int(
                    (
                        (ctx["fire_events"]["component"] == c)
                        & (
                            pd.to_datetime(ctx["fire_events"]["start_at"])
                            >= pd.Timestamp("2025-07-01")
                        )
                    ).sum()
                )
                for c in ("a", "b1")
            },
            "blocked_days": sorted(
                d
                for d in pd.to_datetime(ctx["blocked"]["day"]).dt.strftime("%Y-%m-%d").unique()
                if d >= "2025-06-30"
            ),
        },
        "seconds": round(perf_counter() - started, 1),
        **provenance(),
    }
    write_json(HOLDOUT_OUT, result)
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    points = report.pop("points")
    report["holdout_v9"] = result
    report["points"] = points
    write_json(REPORT, report, one_line="points")
    log_event("holdout_finished", report=str(REPORT.relative_to(ROOT)))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", choices=["dev", "freeze", "holdout", "repeat-fix"], required=True
    )
    parser.add_argument("--write-draft-config", action="store_true")
    parser.add_argument(
        "--dry-run", action="store_true", help="holdout code on 2023/2024 only (before freeze)"
    )
    parser.add_argument("--repeat", action="store_true", help="the one repeat after a fix")
    args = parser.parse_args()
    if args.stage == "freeze":
        print(json.dumps(freeze(), ensure_ascii=False, indent=1))
        return
    if args.stage == "repeat-fix":
        print(json.dumps(log_repeat_fix(), ensure_ascii=False, indent=1))
        return
    if args.stage == "holdout":
        if args.dry_run:
            out = dry_run()
            print(json.dumps(ev._py(out), ensure_ascii=False, indent=1)[:4000])
            if not (out["check"]["all_equal"] and out["bootstrap_point_equals_point"]):
                raise SystemExit("dry run failed")
            return
        out = run_holdout(args.repeat)
        print(json.dumps(ev._py(out["criterion_tz_prod_phase"]), ensure_ascii=False, indent=1))
        print(f"holdout: {HOLDOUT_OUT} ({out['seconds']} s)")
        return
    result = run_dev()
    local = {**result, **provenance()}
    write_json(DEV_POINTS, local)
    report = {
        "stage": "v9 phase 1: development folds 2023/2024 only; the holdout was not read",
        "note": "selection on the same folds that the explorations used; a point has to be "
        "confirmed on the holdout after the freeze (fifth use) or on data after 06.2026",
        "target": TARGET,
        "scopes": SCOPE_TEXT,
        "demo_mask": "S1, S4 and fire are computed without and with the TO-session mask of "
        f"smoke and heat detectors ({MASK_VERSION}); scope labels with '+mask'; "
        "'fire_detector' is the DS-A target a + b1 on the unit object x fire subsystem",
        "points_format": POINT_FIELDS,
        **{k: v for k, v in local.items() if k != "points"},
        "points": [compact(p) for p in local["points"]],
        "full_points": "artifacts/next-2026-09-27/v9/dev_points.json (local)",
    }
    write_json(REPORT, report, one_line="points")
    if args.write_draft_config:
        config = draft_config(result["selection"])
        existing = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
        if str(existing.get("status", "")).startswith("frozen"):
            raise SystemExit("the v9 config is frozen; the draft is not overwritten")
        write_json(CONFIG, config)
    print(json.dumps(ev._py(result["selection"]), ensure_ascii=False, indent=1)[:6000])
    print(f"report: {REPORT} ({result['seconds']} s)")


if __name__ == "__main__":
    main()

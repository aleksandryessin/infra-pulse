"""v5 exploration after stage 1 (team decision 26.09.2026). No refit: only the saved
card scores of the 10 v5 models (gitignored ``artifacts/sensor-failure-v1/tuning_v5``)
and the rule references. Evaluation years 2023/2024 of the development folds; the
calibration period and the holdout are never read. This is a selection on the folds:
a final check needs its own pre-registration.

1. Budget curve of the rolling list with release, at most K open: static list,
   persistence, the models and the combinations of item 2; the points nearest to
   P >= 0.7 and R >= 0.5, the best F1 and the largest recall with P >= 0.7.
2. Combinations with the static list (within-day ranks as the v2 ``rank_mean``):
   rank mean of F2 and the list, rank mean of F2+sel and the list, the list with ties
   inside one event count broken by F2. Paired week and object bootstrap against the
   list at every K.
3. "All episodes" without models (none were trained): the same curve for the static
   list and persistence.
4. Frequency calibration of the list: event count of the pair in the past 365 days ->
   share of cards with an event in the window; bins with n >= 30 (and coarse bins with
   n >= 100) and Wilson intervals, built on one year and checked on the other; cards
   issued by the list under the main policy and all pairs at cutoffs D days apart.

    python data-science/scripts/explore_sensor_failure_v5.py
"""

from __future__ import annotations

import argparse
import json
import runpy
from math import hypot
from pathlib import Path
from time import perf_counter

import numpy as np
import pandas as pd

from infra_pulse_research.modeling import sensor_failure_evaluation as ev
from infra_pulse_research.modeling.sensor_failure_nodes import select_v4
from infra_pulse_research.modeling.sensor_failure_tuning import day_rank
from infra_pulse_research.modeling.target_audit import wilson_interval

RUNNER = Path(__file__).resolve().parent / "tune_sensor_failure_v5.py"
V5 = runpy.run_path(str(RUNNER), run_name="v5")
ROOT = V5["ROOT"]
OUT = V5["OUT"]
REPORT = ROOT / "data-science/reports/sensor-failure-v5-exploration-2026-09-26.json"
TARGET = "connection_loss_ge2s"
BUDGETS = {240: (5, 6, 7, 8, 10), 336: (5, 6, 7, 8, 10, 12, 14)}
GOAL = {"card_precision": 0.7, "event_recall": 0.5}
MIN_BIN = 30
COARSE_BIN = 100  # added beside the requested n >= 30: is a coarser table stable?
COMBINATIONS = {
    "rank_mean F2+list": "mean of the within-day ranks of F2 and the static list",
    "rank_mean F2+sel+list": "mean of the within-day ranks of F2+sel and the static list",
    "list, ties by F2": "static list; ties inside one event count ordered by F2",
}
NOTE = (
    "exploration after stage 1 on the development folds (evaluation 2023 and 2024), "
    "no refit; choices made here are a selection on these folds, and a final check "
    "needs its own pre-registration; calibration and holdout were not read"
)


def release(days: int, k: int) -> dict:
    return {"type": "rolling_release", "max_open": k, "window_days": days}


def add_combinations(cards: pd.DataFrame) -> pd.DataFrame:
    """Combinations of item 2; ranks are within one issue day (as v2 ``rank_mean``)."""
    cards = cards.copy()
    listed = day_rank(cards, "static_list")
    cards["rank_mean F2+list"] = 0.5 * (day_rank(cards, "F2") + listed)
    cards["rank_mean F2+sel+list"] = 0.5 * (day_rank(cards, "F2+sel") + listed)
    # Counts are integers and the rank is in (0, 1]: half of it never crosses a count.
    cards["list, ties by F2"] = cards["static_list"] + 0.5 * day_rank(cards, "F2")
    return cards


def mask_metrics(cards, links, events, mask: np.ndarray) -> dict:
    """``sensor_failure_nodes.policy_metrics`` for a given issue mask."""
    evaluable = cards["outcome"].isin(ev.EVALUABLE).to_numpy()
    y = cards["y_card"].to_numpy(dtype=float)[evaluable]
    sel = mask[evaluable]
    out = ev.event_outcomes(cards, links, events, mask)
    out = out[out["evaluable"]]
    lead = out.loc[out["captured"], "lead_hours"].to_numpy(dtype=float)
    days = pd.Series(mask.astype(int)).groupby(cards["issued_at"].to_numpy()).sum()
    return {
        "cards_selected": int(sel.sum()),
        "card_precision": float(y[sel].mean()) if sel.any() else None,
        "event_recall": float(out["captured"].mean()) if len(out) else None,
        "lead_hours_median": float(np.median(lead)) if len(lead) else None,
        "new_cards_per_day": float(days.mean()) if len(days) else None,
    }


def oracle(cards, links, events) -> np.ndarray:
    linked = links[links["candidate_member"].astype(bool)]
    linked = linked[linked["event_id"].isin(events.loc[events["qualifies"], "event_id"])]
    counts = linked.groupby(V5["KEYS"]).event_id.nunique().rename("_events")
    frame = cards[V5["KEYS"]].merge(counts, on=V5["KEYS"], how="left")
    return cards["y_card"].fillna(0).to_numpy() * (1 + frame["_events"].fillna(0).to_numpy())


def f1(p, r):
    return 2 * p * r / (p + r) if p is not None and r is not None and p + r > 0 else None


def curve(folds, events, scores, days, budgets) -> tuple[dict, dict]:
    """Per score and K: fold metrics and mean; masks per fold, concatenated."""
    points, masks = {}, {}
    for k in budgets:
        policy = release(days, k)
        masks[k] = {}
        for score in [*scores, "ceiling"]:
            fold_masks, fold_metrics = [], []
            for cards, links in folds:
                if score == "ceiling":
                    frame = cards.assign(_oracle=oracle(cards, links, events))
                    mask = select_v4(frame, "_oracle", policy)
                else:
                    mask = select_v4(cards, score, policy)
                fold_masks.append(mask)
                fold_metrics.append(mask_metrics(cards, links, events, mask))
            masks[k][score] = np.concatenate(fold_masks)
            mean = {
                m: float(np.mean([f[m] for f in fold_metrics]))
                for m in fold_metrics[0]
                if m != "cards_selected" and all(f[m] is not None for f in fold_metrics)
            }
            mean["f1"] = f1(mean.get("card_precision"), mean.get("event_recall"))
            points.setdefault(score, {})[str(k)] = {"folds": fold_metrics, "mean": mean}
    return points, masks


def best_points(points: dict) -> dict:
    """Nearest to the goal region, best F1 and the largest recall with P >= 0.7 (means)."""
    rows = [
        (score, int(k), v["mean"]["card_precision"], v["mean"]["event_recall"], v["mean"]["f1"])
        for score, by_k in points.items()
        if score != "ceiling"
        for k, v in by_k.items()
    ]

    def gap(row):
        return hypot(
            max(0.0, GOAL["card_precision"] - row[2]), max(0.0, GOAL["event_recall"] - row[3])
        )

    def show(row):
        return {"score": row[0], "k": row[1], "card_precision": row[2], "event_recall": row[3]}

    nearest = min(rows, key=lambda r: (gap(r), -r[4]))
    precise = [r for r in rows if r[2] >= GOAL["card_precision"]]
    per_score = {}
    for score in {r[0] for r in rows}:
        own = [r for r in rows if r[0] == score]
        own_precise = [r for r in own if r[2] >= GOAL["card_precision"]]
        per_score[score] = {
            "best_f1_k": max(own, key=lambda r: r[4])[1],
            "max_recall_with_p70_k": max(own_precise, key=lambda r: r[3])[1]
            if own_precise
            else None,
        }
    return {
        "goal": GOAL,
        "goal_reached": [show(r) for r in rows if gap(r) == 0],
        "nearest": {**show(nearest), "gap": gap(nearest)},
        "best_f1": {**show(max(rows, key=lambda r: r[4])), "f1": max(r[4] for r in rows)},
        "max_recall_with_p70": show(max(precise, key=lambda r: r[3])) if precise else None,
        "per_score": per_score,
    }


def load_ge2s(horizon: int) -> tuple[list, list[str]]:
    folds, sets = [], []
    for i in range(2):
        cards = pd.read_parquet(OUT / f"{horizon}h" / f"cards_fold{i}.parquet")
        links = pd.read_parquet(OUT / f"{horizon}h" / f"links_fold{i}.parquet")
        folds.append((add_combinations(cards), links))
    fits = json.loads((OUT / f"{horizon}h" / "fits.json").read_text(encoding="utf-8"))
    sets = fits["sets"]
    return folds, sets


def load_rules(con, target: str, rule, horizon: int) -> list:
    """Cards of a threshold with persistence and the static list (as stage 0)."""
    _, members = V5["events_of"](con, target)
    folds = []
    for year in (2023, 2024):
        months = V5["year_months"](year)
        chan = V5["channel_persistence"](con, months)
        cards, links = V5["cards_of"](con, target, horizon, months)
        cards = ev.attach_card_scores(cards, chan, ["persistence"])
        cards["static_list"] = ev.static_list_scores(cards, members, rule).to_numpy()
        folds.append((cards, links))
    return folds


# -- calibration of the static list ----------------------------------------------------


def bin_edges(counts: np.ndarray, min_n: int = MIN_BIN) -> list[float]:
    """Lower edges of consecutive count bins, each with at least ``min_n`` cards; a
    remainder below ``min_n`` joins the last bin, which is open upwards."""
    values, sizes = np.unique(counts, return_counts=True)
    edges, n = [], 0
    for value, size in zip(values, sizes, strict=True):
        if n == 0:
            edges.append(float(value))
        n += int(size)
        if n >= min_n:
            n = 0
    if n and len(edges) > 1:
        edges.pop()
    return edges


def bin_table(counts, y, edges) -> list[dict]:
    index = np.clip(np.searchsorted(edges, counts, side="right") - 1, 0, len(edges) - 1)
    rows = []
    for b, low in enumerate(edges):
        part = y[index == b]
        high = edges[b + 1] - 1 if b + 1 < len(edges) else None
        rows.append(
            {
                "counts": [low, high],
                "n": len(part),
                "k": int(part.sum()),
                **{
                    f"wilson_{k}": v
                    for k, v in wilson_interval(int(part.sum()), len(part)).items()
                    if k != "blocks"
                },
            }
        )
    return rows


def calibration(build, test, min_n: int = MIN_BIN) -> dict:
    """Bins and rates from the build year; the same bins on the test year."""
    edges = bin_edges(build["count"].to_numpy(), min_n)
    fit = bin_table(build["count"].to_numpy(), build["y"].to_numpy(dtype=float), edges)
    check = bin_table(test["count"].to_numpy(), test["y"].to_numpy(dtype=float), edges)
    rates = [r["wilson_point"] for r in fit]
    drops = [i for i in range(1, len(rates)) if rates[i] < rates[i - 1]]
    significant = [i for i in drops if fit[i]["wilson_high"] < fit[i - 1]["wilson_low"]]
    usable = [(f, c) for f, c in zip(fit, check, strict=True) if c["n"] > 0]
    inside = [f["wilson_low"] <= c["wilson_point"] <= f["wilson_high"] for f, c in usable]
    overlap = [
        f["wilson_low"] <= c["wilson_high"] and c["wilson_low"] <= f["wilson_high"]
        for f, c in usable
    ]
    n_test = sum(c["n"] for _, c in usable)
    return {
        "bins": [
            {"counts": f["counts"], "build": f, "test": c} for f, c in zip(fit, check, strict=True)
        ],
        "monotone_build": not drops,
        "drops_build": len(drops),
        "drops_outside_wilson": len(significant),
        "test_rate_inside_build_wilson": f"{sum(inside)}/{len(inside)}",
        "wilson_overlap": f"{sum(overlap)}/{len(overlap)}",
        "weighted_abs_gap": float(
            sum(c["n"] * abs(c["wilson_point"] - f["wilson_point"]) for f, c in usable) / n_test
        )
        if n_test
        else None,
    }


def calibration_frames(folds, days) -> dict:
    """Two populations per year: cards issued by the list under the main policy, and
    every evaluable pair at cutoffs D days apart (windows of one pair do not overlap)."""
    frames = {"issued_by_list": [], "all_pairs_every_d_days": []}
    for cards, _ in folds:
        evaluable = cards["outcome"].isin(ev.EVALUABLE).to_numpy()
        mask = select_v4(cards, "static_list", release(days, days))
        issued = cards.loc[mask & evaluable]
        frames["issued_by_list"].append(
            pd.DataFrame({"count": issued["static_list"], "y": issued["y_card"]})
        )
        day = pd.to_datetime(cards["issued_at"])
        step = ((day - day.min()).dt.days % days == 0).to_numpy()
        spaced = cards.loc[step & evaluable]
        frames["all_pairs_every_d_days"].append(
            pd.DataFrame({"count": spaced["static_list"], "y": spaced["y_card"]})
        )
    out = {}
    for name, (y2023, y2024) in frames.items():
        out[name] = {"cards": [len(y2023), len(y2024)]}
        for min_n in (MIN_BIN, COARSE_BIN):
            out[name][f"min_n_{min_n}"] = {
                "build_2023_test_2024": calibration(y2023, y2024, min_n),
                "build_2024_test_2023": calibration(y2024, y2023, min_n),
            }
    return out


# -- main ------------------------------------------------------------------------------


def explore(replicates: int) -> dict:
    started = perf_counter()
    con = V5["_con"]()
    events, _ = V5["events_of"](con, TARGET)
    result = {
        "stage": "exploration after stage 1 (no refit)",
        "note": NOTE,
        "target": TARGET,
        "policy": "rolling list with release, at most K open (window = horizon)",
        "budgets": {f"{h}h": list(k) for h, k in BUDGETS.items()},
        "combinations": COMBINATIONS,
        "calibration_rule": f"bins of consecutive counts with n >= {MIN_BIN} (requested) and "
        f"n >= {COARSE_BIN} (coarse, added) in the build year, Wilson 95%; Wilson assumes "
        "independent cards, but cards of one pair repeat, so the intervals are optimistic",
        "ge2s": {},
        "all_episodes_rules": {},
    }
    for horizon, budgets in BUDGETS.items():
        days = V5["HORIZONS"][horizon]
        folds, sets = load_ge2s(horizon)
        scores = ["static_list", "persistence", *sets, *COMBINATIONS]
        points, masks = curve(folds, events, scores, days, budgets)
        cards = pd.concat([c for c, _ in folds], ignore_index=True)
        links = pd.concat([lk for _, lk in folds], ignore_index=True)
        others = [s for s in scores if s != "static_list"]
        boot = {}
        for k in budgets:
            boot[str(k)] = V5["bootstrap_masks"](
                cards,
                links,
                events,
                {s: masks[k][s] for s in scores},
                [(s, "static_list", f"{s} - static_list") for s in others],
                replicates=replicates,
                seed=17,
            )
        result["ge2s"][f"{horizon}h"] = {
            "sets": sets,
            "curve": points,
            "best": best_points(points),
            "bootstrap_vs_list": boot,
            "calibration_static_list": calibration_frames(folds, days),
        }
        print(f"ge2s {horizon}h done ({round(perf_counter() - started)} s)", flush=True)
    target_all, rule_all = V5["THRESHOLDS"]["all"]
    events_all, _ = V5["events_of"](con, target_all)
    for horizon, budgets in BUDGETS.items():
        days = V5["HORIZONS"][horizon]
        folds = load_rules(con, target_all, rule_all, horizon)
        points, _ = curve(folds, events_all, ["static_list", "persistence"], days, budgets)
        result["all_episodes_rules"][f"{horizon}h"] = {"curve": points, "best": best_points(points)}
        print(f"all {horizon}h done ({round(perf_counter() - started)} s)", flush=True)
    result["seconds"] = round(perf_counter() - started, 1)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replicates", type=int, default=400)
    args = parser.parse_args()
    result = {**explore(args.replicates), **V5["_provenance"]()}
    V5["_write"](REPORT, result)
    print(f"report: {REPORT} ({result['seconds']} s)")


if __name__ == "__main__":
    main()

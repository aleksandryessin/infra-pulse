"""v5 exploration 4 (team decision): an exact K grid and the plan policy. No training;
evaluation years 2023/2024 of the development folds; the calibration period and the
holdout are never read. This is a selection on the folds: a final check needs its own
pre-registration.

Question: is there a point where, in every fold, card precision >= 0.7 and the honest
recall (an event counts only if a selected card of the pair is open when it starts) >= 0.5?

- horizons 14, 21, 28 and 30 days; K from 10 to D with step 1; targets >= 2 s and all;
- policies: ``release`` (a card leaves at its first event, its place is freed) and
  ``keep`` (a card stays open to the end of its window, the place is freed then);
- scores: static list, list + persistence (rank mean), list with a 90-day decay; at
  14 days and >= 2 s also the rank mean of F2 and the list (saved stage 1 scores);
- per point and fold: precision, honest recall, protocol recall, new cards a day,
  median honest lead, share of chronic cards among the issued ones.

    python data-science/scripts/explore_sensor_failure_v5_part4.py
"""

from __future__ import annotations

import argparse
import runpy
from math import hypot
from pathlib import Path
from time import perf_counter

import numpy as np
import pandas as pd

from infra_pulse_research.modeling import sensor_failure_evaluation as ev
from infra_pulse_research.modeling.sensor_failure_nodes import select_v4
from infra_pulse_research.modeling.sensor_failure_tuning import day_rank

HERE = Path(__file__).resolve().parent
P3 = runpy.run_path(str(HERE / "explore_sensor_failure_v5_part3.py"), run_name="p3")
P2, X, V5 = P3["P2"], P3["X"], P3["V5"]
ROOT, SF, OUT = V5["ROOT"], V5["SF"], V5["OUT"]
REPORT = ROOT / "data-science/reports/sensor-failure-v5-exploration4-2026-09-27.json"
LABEL_DIRS = {
    336: "model_labels_v5",
    504: "model_labels_v5_long",
    672: "model_labels_v5_28d",
    720: "model_labels_v5_long",
}
HORIZON_DAYS = (14, 21, 28, 30)
K_MIN = 10
TARGETS = {"ge2s": V5["THRESHOLDS"]["ge2s"], "all": V5["THRESHOLDS"]["all"]}
POLICIES = {"release": "rolling_release", "keep": "rolling"}
GOAL = X["GOAL"]
KEYS = ["object_id", "sensor_type", "issued_at"]
NEAREST = 5


def policy(kind: str, days: int, k: int) -> dict:
    return {"type": POLICIES[kind], "max_open": k, "window_days": days}


def cards_links(con, days: int, target: str, year: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    root = SF / LABEL_DIRS[days * 24]
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


def add_scores(cards: pd.DataFrame, members, rule) -> pd.DataFrame:
    """List + persistence, the list with a 90-day decay (history without 2021) and, when
    saved F2 scores exist, the rank mean of F2 and the list."""
    cards = cards.copy()
    times = P2["unit_event_times"](P2["without_year"](members), rule)
    if not np.array_equal(P2["window_counts"](cards, times, 365), cards["static_list"].to_numpy()):
        raise ValueError("flat list rebuilt from the variant history differs")
    listed = day_rank(cards, "static_list")
    cards["list+persistence"] = 0.5 * (listed + day_rank(cards, "persistence"))
    cards["list decay 90d"] = P2["decayed_counts"](cards, times, 90)
    if "F2" in cards:
        cards["rank_mean F2+list"] = 0.5 * (day_rank(cards, "F2") + listed)
    return cards


def honest_outcomes(cards, links, events, mask: np.ndarray, days: int, keep: bool) -> pd.DataFrame:
    """Evaluable events with the protocol capture, the honest capture (a selected card of
    the pair is open when the event starts) and the honest lead from the earliest open
    capturing card. Under ``release`` a card is open until the day after its first event;
    under ``keep`` until the end of its window."""
    out = ev.event_outcomes(cards, links, events, mask)
    out = out[out["evaluable"]].copy()
    sel = cards.loc[mask, [*KEYS, "first_event_start", "y_card"]].copy()
    issued = pd.to_datetime(sel["issued_at"])
    end = issued + pd.Timedelta(days=days)
    if keep:
        sel["release"] = end
    else:
        start = pd.to_datetime(sel["first_event_start"]).where(sel["y_card"] == 1)
        release = (start.dt.floor("D") + pd.Timedelta(days=1)).fillna(end)
        sel["release"] = release.where(release <= end, end)
    linked = links[links["candidate_member"].astype(bool)].merge(sel, on=KEYS, how="inner")
    starts = events[["event_id", "event_start"]].rename(columns={"event_start": "_start"})
    linked = linked.merge(starts, on="event_id")
    start_at = pd.to_datetime(linked["_start"])
    linked = linked[(start_at < linked["release"]) & (start_at >= linked["issued_at"])]
    # The earliest open card that captures the event: its lead, and whether the event is
    # a repeat (the card already had an earlier event in its window).
    first = linked.sort_values(["event_id", "issued_at"]).groupby("event_id").first()
    first["lead_open"] = (
        pd.to_datetime(first["_start"]) - pd.to_datetime(first["issued_at"])
    ).dt.total_seconds() / 3600
    first["repeat"] = pd.to_datetime(first["_start"]) > pd.to_datetime(first["first_event_start"])
    out = out.merge(
        first[["lead_open", "repeat"]], left_on="event_id", right_index=True, how="left"
    )
    out["captured_open"] = out["lead_open"].notna()
    return out


def point(cards, links, events, mask, days, keep, chronic_prev) -> dict:
    evaluable = cards["outcome"].isin(ev.EVALUABLE).to_numpy()
    y = cards["y_card"].to_numpy(dtype=float)
    chosen = mask & evaluable
    h = honest_outcomes(cards, links, events, mask, days, keep)
    caught = h[h["captured_open"]]
    lead = caught["lead_open"].to_numpy(dtype=float)
    per_day = pd.Series(mask.astype(int)).groupby(cards["issued_at"].to_numpy()).sum()
    return {
        "cards_selected": int(chosen.sum()),
        "card_precision": float(y[chosen].mean()) if chosen.any() else None,
        "event_recall_open": float(h["captured_open"].mean()) if len(h) else None,
        "event_recall_protocol": float(h["captured"].mean()) if len(h) else None,
        "new_cards_per_day": float(per_day.mean()),
        "lead_hours_median_open": float(np.median(lead)) if len(lead) else None,
        "chronic_share_issued": float(chronic_prev[chosen].mean()) if chosen.any() else None,
        "repeat_share_captured": float(caught["repeat"].astype(bool).mean())
        if len(caught)
        else None,
    }


def summarize(folds: list[dict]) -> dict:
    keys = [k for k in folds[0] if k != "cards_selected"]
    mean = {
        k: float(np.mean([f[k] for f in folds]))
        for k in keys
        if all(f[k] is not None for f in folds)
    }
    p = min(f["card_precision"] for f in folds)
    r = min(f["event_recall_open"] for f in folds)
    gap = hypot(max(0.0, GOAL["card_precision"] - p), max(0.0, GOAL["event_recall"] - r))
    return {
        "folds": folds,
        "mean": mean,
        "min_precision": p,
        "min_recall_open": r,
        "gap_worst_fold": gap,
    }


def explore() -> dict:
    started = perf_counter()
    con = V5["_con"]()
    persistence = {y: V5["channel_persistence"](con, V5["year_months"](y)) for y in (2023, 2024)}
    grid, checks, count = {}, {"keep_open_equals_protocol_max_abs": 0.0}, 0
    for tname, (target, rule) in TARGETS.items():
        events, members = V5["events_of"](con, target)
        for days in HORIZON_DAYS:
            folds = []
            for i, year in enumerate((2023, 2024)):
                if days == 14 and tname == "ge2s":
                    cards = pd.read_parquet(OUT / "336h" / f"cards_fold{i}.parquet")
                    links = pd.read_parquet(OUT / "336h" / f"links_fold{i}.parquet")
                else:
                    cards, links = cards_links(con, days, target, year)
                    cards = ev.attach_card_scores(cards, persistence[year], ["persistence"])
                    cards["static_list"] = ev.static_list_scores(cards, members, rule).to_numpy()
                cards = add_scores(cards, members, rule)
                prev = ev.static_list_scores(cards, members, rule, window_days=days).to_numpy() > 0
                folds.append((cards, links, prev))
            scores = ["static_list", "list+persistence", "list decay 90d"]
            if "rank_mean F2+list" in folds[0][0]:
                scores.append("rank_mean F2+list")
            block = {}
            for kind in POLICIES:
                keep = kind == "keep"
                block[kind] = {}
                for score in scores:
                    block[kind][score] = {}
                    for k in range(K_MIN, days + 1):
                        per_fold = []
                        for cards, links, prev in folds:
                            mask = select_v4(cards, score, policy(kind, days, k))
                            per_fold.append(point(cards, links, events, mask, days, keep, prev))
                            if keep:
                                diff = abs(
                                    per_fold[-1]["event_recall_open"]
                                    - per_fold[-1]["event_recall_protocol"]
                                )
                                checks["keep_open_equals_protocol_max_abs"] = max(
                                    checks["keep_open_equals_protocol_max_abs"], diff
                                )
                        block[kind][score][str(k)] = summarize(per_fold)
                        count += 1
            grid[f"{tname} {days}d"] = {"scores": scores, "policies": block}
            print(f"{tname} {days}d done ({round(perf_counter() - started)} s)", flush=True)
    rows = []
    for name, block in grid.items():
        tname, horizon = name.split()
        for kind, by_score in block["policies"].items():
            for score, by_k in by_score.items():
                for k, v in by_k.items():
                    rows.append(
                        {
                            "target": tname,
                            "days": int(horizon.removesuffix("d")),
                            "policy": kind,
                            "score": score,
                            "k": int(k),
                            "min_precision": v["min_precision"],
                            "min_recall_open": v["min_recall_open"],
                            "gap_worst_fold": v["gap_worst_fold"],
                            "new_cards_per_day": v["mean"]["new_cards_per_day"],
                            "folds": [
                                {m: f[m] for m in ("card_precision", "event_recall_open")}
                                for f in v["folds"]
                            ],
                            "lead_hours_median_open": v["mean"].get("lead_hours_median_open"),
                            "chronic_share_issued": v["mean"].get("chronic_share_issued"),
                            "repeat_share_captured": v["mean"].get("repeat_share_captured"),
                        }
                    )
    reached = sorted(
        (r for r in rows if r["gap_worst_fold"] == 0),
        key=lambda r: (r["days"], r["new_cards_per_day"]),
    )
    nearest = sorted(rows, key=lambda r: (r["gap_worst_fold"], r["days"], r["new_cards_per_day"]))
    per_setting = {}
    for r in rows:
        key = f"{r['target']} {r['days']}d {r['policy']}"
        if key not in per_setting or r["gap_worst_fold"] < per_setting[key]["gap_worst_fold"]:
            per_setting[key] = r
    return {
        "stage": "exploration 4 — exact K grid and the plan policy (no training)",
        "note": X["NOTE"],
        "question": "a point where in every fold P >= 0.7 and the honest recall >= 0.5",
        "labels": {
            "14 days": "model_labels_v5 (>= 2 s: saved stage 1 cards with F2)",
            "21, 30 days": "model_labels_v5_long",
            "28 days": "model_labels_v5_28d (configs/sensor_failure_model_v5_28d.json)",
        },
        "policies": {
            "release": "a card leaves the list the day after its first event; its place is freed",
            "keep": "a card stays open to the end of its window; repeated events of the pair "
            "inside the window count; the place is freed at the window end",
        },
        "recall": "honest: an evaluable event counts only if a selected card of the pair is "
        "open when it starts; the protocol recall is kept beside it",
        "lead": "honest: from the earliest selected card that is open when the event starts",
        "chronic": "the pair had an event in [t - D, t) (counted as the static list)",
        "repeat": "a captured event that starts after the first event of its earliest open "
        "capturing card (recall counts events, so one card of a chronic pair can capture several)",
        "combinations_checked": count,
        "checks": checks,
        "grid": grid,
        "summary": {
            "goal_in_every_fold": reached,
            "nearest": nearest[:NEAREST],
            "best_per_target_horizon_policy": per_setting,
        },
        "seconds": round(perf_counter() - started, 1),
    }


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    result = {**explore(), **V5["_provenance"]()}
    V5["_write"](REPORT, result)
    print(f"report: {REPORT} ({result['seconds']} s)")


if __name__ == "__main__":
    main()

"""v5 exploration 2 (team decision 26.09.2026). No training: rules, and the saved card
scores of the v5 models where they exist (14 days, >= 2 s). Evaluation years 2023/2024 of
the development folds; the calibration period and the holdout are never read. This is a
selection on the folds: a final check needs its own pre-registration.

1. Horizons 5, 7, 10, 14, 15, 21 and 30 days, targets >= 2 s and all episodes, rolling list
   with release, K in {5, 8, 10, 12, 14, D}: static list, persistence, ceiling; where
   precision levels off and how the share of "chronic" cards grows (the pair had an event
   in the previous window of the same length).
2. 14 days with a finer K: 8..14.
3. Variants of the static list at 14 days (>= 2 s and all episodes): decayed count with a
   half-life of 30, 90, 180 days; history windows 180 and 730 days; the count over the
   co-failure node of v4 (monthly point-in-time snapshots); rank mean of the list and
   persistence. Paired week and object bootstrap against the flat list at K = 10 and 12.
   History of the variants excludes 2021 (outside the training data of the project).
4. Best points to P >= 0.7 and R >= 0.5: rules, and rules with the model (14 days, >= 2 s).

    python data-science/scripts/explore_sensor_failure_v5_part2.py
"""

from __future__ import annotations

import argparse
import runpy
from pathlib import Path
from time import perf_counter

import numpy as np
import pandas as pd

from infra_pulse_research.modeling import sensor_failure_evaluation as ev
from infra_pulse_research.modeling.sensor_failure_nodes import select_v4
from infra_pulse_research.modeling.sensor_failure_tuning import day_rank

HERE = Path(__file__).resolve().parent
X = runpy.run_path(str(HERE / "explore_sensor_failure_v5.py"), run_name="x")
V5 = X["V5"]
ROOT = V5["ROOT"]
SF = V5["SF"]
REPORT = ROOT / "data-science/reports/sensor-failure-v5-exploration2-2026-09-26.json"
LABEL_DIRS = {"model_labels_v5": (120, 168, 240, 336), "model_labels_v5_long": (360, 504, 720)}
HORIZONS = {h: h // 24 for hs in LABEL_DIRS.values() for h in hs}
TARGETS = {"ge2s": V5["THRESHOLDS"]["ge2s"], "all": V5["THRESHOLDS"]["all"]}
FINE_K = (8, 9, 10, 11, 12, 13, 14)
BOOT_K = (10, 12)
HALF_LIVES = (30, 90, 180)
WINDOWS = (180, 730)
EXCLUDED_YEAR = 2021
REFERENCE = pd.Timestamp("2019-01-01")
UNIT = ["object_id", "sensor_type"]
NOTE = X["NOTE"]


def sweep_budgets(days: int) -> list[int]:
    return sorted({5, 8, 10, 12, 14, days})


def labels_dir(horizon: int) -> Path:
    name = next(d for d, hs in LABEL_DIRS.items() if horizon in hs)
    return SF / name


def cards_links(con, horizon: int, target: str, year: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    root = labels_dir(horizon)
    out = []
    for kind in ("cards", "links"):
        files = V5["_files"](
            root / f"{kind}/{target}/{horizon}h/{m}.parquet" for m in V5["year_months"](year)
        )
        out.append(
            con.execute(
                f"SELECT * FROM read_parquet({files}) "
                f"WHERE issued_at < TIMESTAMP {V5['_q'](V5['DEV_END'])}"
            ).df()
        )
    return out[0].reset_index(drop=True), out[1]


# -- history of a unit -----------------------------------------------------------------


def unit_event_times(members: pd.DataFrame, rule, key=("object_id", "sensor_type")) -> pd.DataFrame:
    """Per event and unit, the moment it counts (as ``static_list_scores``): the later of
    the unit's first member start and the moment the event is known to qualify."""
    key = list(key)
    m = members.copy()
    m["start_at"] = pd.to_datetime(m["start_at"])
    known = ev._known_after(m["start_at"], rule).where(m["qualifying"].astype(bool))
    event_known = known.groupby(m["event_id"]).min().rename("event_known")
    first = m.groupby(["event_id", *key])["start_at"].min().rename("first").reset_index()
    first = first.merge(event_known, left_on="event_id", right_index=True, how="left")
    first = first.dropna(subset=["event_known"])
    first["count_at"] = first[["first", "event_known"]].max(axis=1)
    return first[["event_id", *key, "count_at"]]


def without_year(members: pd.DataFrame, year: int = EXCLUDED_YEAR) -> pd.DataFrame:
    return members[pd.to_datetime(members["start_at"]).dt.year != year]


def _per_unit(cards: pd.DataFrame, times: pd.DataFrame, fn) -> np.ndarray:
    stamps = {
        k: np.sort(part["count_at"].to_numpy(dtype="datetime64[ns]"))
        for k, part in times.groupby(UNIT, sort=False)
    }
    issued = pd.to_datetime(cards["issued_at"]).to_numpy(dtype="datetime64[ns]")
    result = np.zeros(len(cards), dtype=float)
    for k, positions in cards.groupby(UNIT, sort=False).indices.items():
        s = stamps.get(k if isinstance(k, tuple) else (k,))
        if s is not None and len(s):
            result[positions] = fn(s, issued[positions])
    return result


def window_counts(cards, times, window_days: int) -> np.ndarray:
    w = np.timedelta64(int(window_days), "D")
    return _per_unit(
        cards,
        times,
        lambda s, t: np.searchsorted(s, t, "left") - np.searchsorted(s, t - w, "left"),
    )


def decayed_counts(cards, times, half_life_days: float) -> np.ndarray:
    """Sum over past events of 2^(-age / half-life); only events counted before t."""
    ref = REFERENCE.to_datetime64()
    day = np.timedelta64(1, "D")

    def fn(s, t):
        x = (s - ref) / day / half_life_days
        prefix = np.concatenate([[0.0], np.cumsum(np.exp2(x))])
        idx = np.searchsorted(s, t, "left")
        return np.exp2(-((t - ref) / day) / half_life_days) * prefix[idx]

    return _per_unit(cards, times, fn)


def node_snapshot(month: str) -> pd.DataFrame:
    return pd.read_parquet(SF / "features_v4" / "nodes" / f"month={month}" / "nodes.parquet")


def unit_channels(target: str, month: str) -> pd.DataFrame:
    return pd.read_parquet(
        labels_dir(336) / "channels" / target / f"{month}.parquet",
        columns=["channel_id", "object_id", "sensor_type"],
    ).drop_duplicates()


def node_counts(
    cards,
    members,
    rule,
    target: str,
    window_days: int = 365,
    *,
    snapshot=node_snapshot,
    channels=unit_channels,
) -> np.ndarray:
    """Distinct events in ``[t - window, t)`` on the unit or on any channel sharing a
    co-failure node with the unit's channels; nodes from the v4 snapshot of the card's
    month (built from starts before the month), counted as ``unit_event_times``."""
    own = unit_event_times(members, rule)
    by_channel = unit_event_times(members, rule, key=("channel_id",))
    result = np.zeros(len(cards), dtype=float)
    for month, positions in cards.groupby("month", sort=True).indices.items():
        snap = snapshot(month).copy()
        snap["node"] = np.where(snap["in_node"].astype(bool), snap["node_id"], snap["channel_id"])
        chans = channels(target, month)
        chans = chans.merge(snap[["channel_id", "node"]], on="channel_id", how="left")
        chans["node"] = chans["node"].fillna(chans["channel_id"])
        grouped = snap.loc[snap["in_node"].astype(bool), ["node", "channel_id"]]
        peers = chans[[*UNIT, "node"]].drop_duplicates().merge(grouped, on="node", how="inner")
        peer_events = peers[[*UNIT, "channel_id"]].merge(by_channel, on="channel_id")
        times = pd.concat([own, peer_events[["event_id", *UNIT, "count_at"]]], ignore_index=True)
        times = times.groupby(["event_id", *UNIT], as_index=False)["count_at"].min()
        result[positions] = window_counts(cards.iloc[positions], times, window_days)
    return result


VARIANTS = {
    **{f"list decay {h}d": f"decayed count, half-life {h} days" for h in HALF_LIVES},
    **{f"list {w}d": f"count over {w} days" for w in WINDOWS},
    "list node 365d": "count over 365 days on the co-failure node of the unit (v4 snapshots)",
    "list+persistence": "mean of the within-day ranks of the list and persistence",
}


def add_variants(cards: pd.DataFrame, members, rule, target: str) -> pd.DataFrame:
    cards = cards.copy()
    times = unit_event_times(without_year(members), rule)
    # The flat 365-day list rebuilt from these times must equal the saved one: the same
    # counting rule, and 2023/2024 cutoffs never reach the excluded year.
    if not np.array_equal(window_counts(cards, times, 365), cards["static_list"].to_numpy()):
        raise ValueError("flat list rebuilt from the variant history differs")
    for h in HALF_LIVES:
        cards[f"list decay {h}d"] = decayed_counts(cards, times, h)
    for w in WINDOWS:
        cards[f"list {w}d"] = window_counts(cards, times, w)
    cards["list node 365d"] = node_counts(cards, without_year(members), rule, target)
    cards["list+persistence"] = 0.5 * (
        day_rank(cards, "static_list") + day_rank(cards, "persistence")
    )
    return cards


def add_model_combos(cards: pd.DataFrame) -> pd.DataFrame:
    """Rules with the model: rank mean of F2 and each list variant (14 days, >= 2 s)."""
    cards = cards.copy()
    f2 = day_rank(cards, "F2")
    for v in [v for v in VARIANTS if v != "list+persistence"]:
        cards[f"rank_mean F2+{v}"] = 0.5 * (f2 + day_rank(cards, v))
    return cards


# -- chronic cards ---------------------------------------------------------------------


def chronic(folds, members, rule, days: int) -> dict:
    """Share of cards whose pair had an event in the previous window of the same length."""
    per_fold = []
    for cards, _ in folds:
        evaluable = cards["outcome"].isin(ev.EVALUABLE).to_numpy()
        prev = ev.static_list_scores(cards, members, rule, window_days=days).to_numpy() > 0
        y = cards["y_card"].to_numpy(dtype=float) == 1
        issued = select_v4(cards, "static_list", X["release"](days, days)) & evaluable
        per_fold.append(
            {
                "evaluable": float(prev[evaluable].mean()),
                "positive": float(prev[evaluable & y].mean()),
                "issued_by_list": float(prev[issued].mean()) if issued.any() else None,
                "precision_issued_chronic": float(y[issued & prev].mean())
                if (issued & prev).any()
                else None,
                "precision_issued_not_chronic": float(y[issued & ~prev].mean())
                if (issued & ~prev).any()
                else None,
            }
        )
    return {
        "folds": per_fold,
        "mean": {
            k: float(np.mean([f[k] for f in per_fold]))
            for k in per_fold[0]
            if all(f[k] is not None for f in per_fold)
        },
    }


# -- stages ----------------------------------------------------------------------------


def horizon_sweep(con, persistence_by_year) -> dict:
    out = {}
    for tname, (target, rule) in TARGETS.items():
        events, members = V5["events_of"](con, target)
        for horizon, days in HORIZONS.items():
            folds = []
            for year in (2023, 2024):
                cards, links = cards_links(con, horizon, target, year)
                cards = ev.attach_card_scores(cards, persistence_by_year[year], ["persistence"])
                cards["static_list"] = ev.static_list_scores(cards, members, rule).to_numpy()
                folds.append((cards, links))
            points, _ = X["curve"](
                folds, events, ["static_list", "persistence"], days, sweep_budgets(days)
            )
            evaluable = [c["outcome"].isin(ev.EVALUABLE) for c, _ in folds]
            out[f"{tname} {days}d"] = {
                "curve": points,
                "chronic": chronic(folds, members, rule, days),
                "base_rate": float(
                    np.mean(
                        [
                            c.loc[e, "y_card"].mean()
                            for (c, _), e in zip(folds, evaluable, strict=True)
                        ]
                    )
                ),
                "excluded_share": [float(1 - e.mean()) for e in evaluable],
            }
    return out


def variants_14d(con, persistence_by_year, replicates: int) -> dict:
    out = {}
    for tname, (target, rule) in TARGETS.items():
        events, members = V5["events_of"](con, target)
        if tname == "ge2s":
            folds, sets = X["load_ge2s"](336)
            folds = [
                (add_model_combos(add_variants(c, members, rule, target)), lk) for c, lk in folds
            ]
            models = [
                *sets,
                *X["COMBINATIONS"],
                *(f"rank_mean F2+{v}" for v in VARIANTS if v != "list+persistence"),
            ]
        else:
            folds = []
            for year in (2023, 2024):
                cards, links = cards_links(con, 336, target, year)
                cards = ev.attach_card_scores(cards, persistence_by_year[year], ["persistence"])
                cards["static_list"] = ev.static_list_scores(cards, members, rule).to_numpy()
                folds.append((add_variants(cards, members, rule, target), links))
            models = []
        rules = ["static_list", "persistence", *VARIANTS]
        scores = [*rules, *models]
        points, masks = X["curve"](folds, events, scores, 14, FINE_K)
        cards = pd.concat([c for c, _ in folds], ignore_index=True)
        links = pd.concat([lk for _, lk in folds], ignore_index=True)
        boot = {
            str(k): V5["bootstrap_masks"](
                cards,
                links,
                events,
                {s: masks[k][s] for s in scores},
                [(s, "static_list", f"{s} - static_list") for s in scores if s != "static_list"],
                replicates=replicates,
                seed=17,
            )
            for k in BOOT_K
        }
        out[tname] = {
            "rules": rules,
            "with_model": models,
            # Share of cards whose node count differs from the flat count: v4 nodes that
            # stay inside one object x sensor type add nothing at unit A.
            "node_count_differs_share": float(
                (cards["list node 365d"] != cards["static_list"]).mean()
            ),
            "curve": points,
            "bootstrap_vs_list": boot,
        }
    return out


def goal_in_every_fold(points: dict) -> list[dict]:
    """(score, K) whose precision and recall meet the goal in each fold, not only on average."""
    goal = X["GOAL"]
    out = []
    for score, by_k in points.items():
        if score == "ceiling":
            continue
        for k, v in by_k.items():
            folds = v["folds"]
            if all(
                f["card_precision"] is not None
                and f["event_recall"] is not None
                and f["card_precision"] >= goal["card_precision"]
                and f["event_recall"] >= goal["event_recall"]
                for f in folds
            ):
                out.append(
                    {
                        "score": score,
                        "k": int(k),
                        "min_precision": min(f["card_precision"] for f in folds),
                        "min_recall": min(f["event_recall"] for f in folds),
                    }
                )
    return out


def summary(sweep: dict, variants: dict) -> dict:
    rules = {}
    for name, block in sweep.items():
        for score in ("static_list", "persistence"):
            rules[f"{score} | {name}"] = block["curve"][score]
    for tname, block in variants.items():
        for score in block["rules"]:
            rules[f"{score} | {tname} 14d fine K"] = block["curve"][score]
    per_horizon = {}
    for name, block in sweep.items():
        subset = {s: block["curve"][s] for s in ("static_list", "persistence")}
        per_horizon[name] = X["best_points"](subset)
    for tname, block in variants.items():
        subset = {s: block["curve"][s] for s in block["rules"]}
        per_horizon[f"{tname} 14d variants"] = X["best_points"](subset)
    ge2s = variants["ge2s"]
    everything = {**rules, **{s: ge2s["curve"][s] for s in ge2s["with_model"]}}
    return {
        "goal_in_every_fold": goal_in_every_fold(everything),
        "rules_all_horizons_and_targets": X["best_points"](rules),
        "rules_per_horizon_target": per_horizon,
        "rules_only_14d_ge2s": X["best_points"]({s: ge2s["curve"][s] for s in ge2s["rules"]}),
        "with_model_only_14d_ge2s": X["best_points"](
            {s: ge2s["curve"][s] for s in ge2s["with_model"]}
        ),
    }


def explore(replicates: int) -> dict:
    started = perf_counter()
    con = V5["_con"]()
    persistence_by_year = {
        y: V5["channel_persistence"](con, V5["year_months"](y)) for y in (2023, 2024)
    }
    sweep = horizon_sweep(con, persistence_by_year)
    print(f"sweep done ({round(perf_counter() - started)} s)", flush=True)
    variants = variants_14d(con, persistence_by_year, replicates)
    print(f"variants done ({round(perf_counter() - started)} s)", flush=True)
    return {
        "stage": "exploration 2 after stage 1 (no training)",
        "note": NOTE,
        "labels": {
            "<= 14 days": "model_labels_v5",
            "15, 21, 30 days": "model_labels_v5_long (configs/sensor_failure_model_v5_long.json)",
        },
        "policy": "rolling list with release, at most K open (window = horizon)",
        "chronic": "the pair had an event (counted as the static list) in [t - D, t)",
        "variants": VARIANTS,
        "variant_history": f"events of {EXCLUDED_YEAR} excluded (outside the project's "
        f"training data); the flat 365-day list of 2023/2024 never reaches {EXCLUDED_YEAR}; "
        "the 730-day window of 2023 cards therefore covers 2022 and part of 2023 only",
        "nodes": "v4 monthly snapshots (co-failure groups from > 2 s starts before the month, "
        "rule min_joint 3, min_ratio 0.8)",
        "horizon_sweep": sweep,
        "variants_14d": variants,
        "summary": summary(sweep, variants),
        "seconds": round(perf_counter() - started, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replicates", type=int, default=400)
    args = parser.parse_args()
    result = {**explore(args.replicates), **V5["_provenance"]()}
    V5["_write"](REPORT, result)
    print(f"report: {REPORT} ({result['seconds']} s)")


if __name__ == "__main__":
    main()

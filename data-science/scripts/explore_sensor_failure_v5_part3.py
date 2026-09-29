"""v5 exploration 3 — 7 days (team decision 26.09.2026). Development folds only
(evaluation years 2023 and 2024); the calibration period and the holdout are never read.
This is a selection on the folds: a final check needs its own pre-registration.

- ``fit --fold i`` — the fits of ``configs/sensor_failure_tuning_v5_7d.json`` for one fold
  (F2 with seeds 17/18/19 and F2+sel with seed 17; target >= 2 s, 168 h), MLflow runs
  with their models; card scores go to gitignored ``artifacts/.../tuning_v5/168h/``.
- ``report`` — (a) 7 days at K in {1, 2, 3, 5, 7, 10}: models (seed mean and spread),
  static list, persistence, list + persistence, rank mean of F2 and the list; paired
  week/object bootstrap against the list; median lead of the model against the list
  (technologist criterion K2); (b) grid horizon x K for rules everywhere and models where
  scores exist (7, 10, 14 days), best point to P >= 0.7 and R >= 0.5 by the worse fold.

    python data-science/scripts/explore_sensor_failure_v5_part3.py --stage fit --fold 0
    python data-science/scripts/explore_sensor_failure_v5_part3.py --stage fit --fold 1
    python data-science/scripts/explore_sensor_failure_v5_part3.py --stage report
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import subprocess
from math import hypot
from pathlib import Path
from time import perf_counter

import numpy as np
import pandas as pd

from infra_pulse_research.modeling import sensor_failure_evaluation as ev
from infra_pulse_research.modeling.sensor_failure_tuning import day_rank

HERE = Path(__file__).resolve().parent
P2 = runpy.run_path(str(HERE / "explore_sensor_failure_v5_part2.py"), run_name="p2")
X = P2["X"]
V5 = P2["V5"]
ROOT = V5["ROOT"]
OUT = V5["OUT"]
CONFIG = ROOT / "data-science/configs/sensor_failure_tuning_v5_7d.json"
REPORT = ROOT / "data-science/reports/sensor-failure-v5-exploration3-2026-09-26.json"
HORIZON, DAYS = 168, 7
DIR = OUT / f"{HORIZON}h"
TARGET, RULE = V5["THRESHOLDS"]["ge2s"]
GRID_DAYS = (5, 7, 10, 14, 15, 21, 30)
GRID_K = (1, 2, 3, 5, 7, 8, 10, 12, 14)
SAVED = {7: "168h", 10: "240h", 14: "336h"}
GOAL = X["GOAL"]
SEED_GROUPS = {"F2": "F2 s{seed}", "rank_mean F2+list": "rank_mean F2 s{seed}+list"}


def model_columns(config: dict) -> list[str]:
    return [f"{f['set']} s{seed}" for f in config["fits"] for seed in f["seeds"]]


def f2_seeds(config: dict) -> list[int]:
    return next(f["seeds"] for f in config["fits"] if f["set"] == "F2")


# -- fits ------------------------------------------------------------------------------


def fit_fold(config: dict, index: int, mlflow_on: bool) -> dict:
    from infra_pulse_research.modeling.sensor_failure_tuning import (  # noqa: PLC0415
        FittedCatBoost,
        card_aggregates,
    )

    started = perf_counter()
    fold = config["folds"][index]
    sets = config["feature_sets"]
    groups = sorted(
        {V5["GROUP_OF_PREFIX"][p] for s in sets.values() for p in s if p in V5["GROUP_OF_PREFIX"]}
    )
    cb = {k: v for k, v in config["catboost"].items() if k != "config"}
    con = V5["_con"]()
    events, members = V5["events_of"](con, TARGET)
    q = V5["_q"]
    ranges = " OR ".join(
        f"(issued_at >= TIMESTAMP {q(a)} AND issued_at < TIMESTAMP {q(b)} - "
        f"INTERVAL {HORIZON} HOUR)"
        for a, b in fold["fit"]
    )
    channels_files = V5["_files"](
        V5["LABELS"] / f"channels/{TARGET}/{m}.parquet" for m in V5["DEV_MONTHS"]
    )
    keys = con.execute(f"""
        SELECT channel_id, issued_at, y_{HORIZON}h::INT AS y FROM read_parquet({channels_files})
        WHERE split_period = 'development' AND candidate
          AND outcome_{HORIZON}h IN ('positive', 'negative') AND ({ranges})
    """).df()
    keep = (keys["y"] == 1).to_numpy() | V5["negative_sample_mask"](keys.channel_id, keys.issued_at)
    con.register("chosen", keys.loc[keep])
    join = V5["_feature_join"](groups, V5["DEV_MONTHS"])
    sample = con.execute(
        f"SELECT chosen.y, * EXCLUDE (y) FROM chosen {join} ORDER BY channel_id, issued_at"
    ).df()
    con.unregister("chosen")
    if len(sample) != int(keep.sum()):
        raise ValueError("features missing for fit rows")
    y = sample.pop("y").to_numpy(dtype=int)
    mlflow = None
    if mlflow_on:
        import mlflow  # noqa: PLC0415

        mlflow.set_tracking_uri(config["mlflow"]["tracking_uri"])
        mlflow.set_experiment(config["mlflow"]["experiment"])
    fitted, runs, fits = {}, {}, []
    for spec in config["fits"]:
        columns = V5["set_columns"](sample, sets[spec["set"]])
        for seed in spec["seeds"]:
            name = f"{spec['set']} s{seed}"
            t1 = perf_counter()
            fitted[name] = FittedCatBoost(sample, y, columns, {}, {**cb, "random_seed": seed})
            info = {
                "fold": index,
                "set": spec["set"],
                "seed": seed,
                "thread_count": cb["thread_count"],
                "fit_rows": len(sample),
                "fit_positive": int(y.sum()),
                "columns": len(columns),
                "seconds": round(perf_counter() - t1, 1),
                "top_features": fitted[name].top_features,
            }
            fits.append(info)
            print(f"168h fold{index} {name}: {info['seconds']} s", flush=True)
            if mlflow is not None:
                run_name = config["mlflow"]["run_name"].format(i=index, set=spec["set"], seed=seed)
                with mlflow.start_run(run_name=run_name) as run:
                    mlflow.set_tags(
                        {
                            "kind": "fit",
                            "is_model": "true",
                            "tuning_version": config["version"],
                            "target": TARGET,
                            "horizon": f"{HORIZON}h",
                            "mlflow.note.content": "Разведка 3 (7 суток) на фолдах разработки; "
                            "не предрегистрация.",
                        }
                    )
                    mlflow.log_params(
                        {k: v for k, v in info.items() if k not in ("seconds", "top_features")}
                    )
                    mlflow.log_metric("fit_seconds", info["seconds"])
                    mlflow.log_dict({"top_features": info["top_features"]}, "top_features.json")
                    mlflow.catboost.log_model(
                        fitted[name].model, name="model", pip_requirements=["catboost"]
                    )
                    runs[name] = run.info.run_id
    del sample
    months = V5["year_months"](int(fold["evaluate"][0][0][:4]))
    parts = []
    for month in months:
        lab = q(V5["LABELS"] / f"channels/{TARGET}/{month}.parquet")
        extra = "".join(f", g_{g}.* EXCLUDE (channel_id, issued_at)" for g in groups)
        frame = con.execute(f"""
            SELECT l.channel_id, l.issued_at, l.object_id, l.sensor_type, l.candidate,
                   f2.* EXCLUDE (channel_id, issued_at, object_id, sensor_type){extra}
            FROM read_parquet({lab}) l {V5["_feature_join"](groups, [month])}
            WHERE l.candidate AND l.issued_at < TIMESTAMP {q(V5["DEV_END"])}
        """).df()
        ch = frame[["channel_id", "issued_at", "object_id", "sensor_type", "candidate"]].copy()
        ch["persistence"] = V5["persistence_score"](frame, V5["LABEL"])
        for name, model in fitted.items():
            ch[name] = model.score(frame)
        parts.append(ch)
    channels = pd.concat(parts, ignore_index=True)
    cards, links = V5["cards_of"](con, TARGET, HORIZON, months)
    table = cards.copy()
    for col, how in [("persistence", "max"), *((s, "mean_top2") for s in fitted)]:
        agg = card_aggregates(channels, col, how).rename(columns={"score": col})
        table = table.merge(agg, on=V5["KEYS"], how="left")
    if len(table) != len(cards):
        raise ValueError("card merge changed rows")
    table["static_list"] = ev.static_list_scores(table, members, RULE).to_numpy()
    DIR.mkdir(parents=True, exist_ok=True)
    table.to_parquet(DIR / f"cards_fold{index}.parquet", index=False)
    links.to_parquet(DIR / f"links_fold{index}.parquet", index=False)
    if mlflow is not None:
        for name, run_id in runs.items():
            with mlflow.start_run(run_id=run_id):
                for k in config_budgets(config):
                    m = V5["policy_metrics"](table, links, events, name, X["release"](DAYS, k))
                    mlflow.log_metrics(
                        {
                            f"release_open{k}_{key}": float(m[key])
                            for key in ("card_precision", "event_recall", "lead_hours_median")
                            if m[key] is not None
                        }
                    )
    result = {
        "fold": index,
        "fits": fits,
        "seconds": round(perf_counter() - started, 1),
        "hygiene": {
            "thread_count": cb["thread_count"],
            "os_cpu_count": os.cpu_count(),
            "cpu_model": subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True
            ).stdout.strip(),
        },
    }
    (DIR / f"fits_fold{index}.json").write_text(
        json.dumps(ev._py(result), ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"168h fold{index} done in {result['seconds']} s", flush=True)
    return result


def config_budgets(config: dict) -> list[int]:
    return list(config["evaluation"]["budgets"])


# -- scores and lead -------------------------------------------------------------------


def add_scores(cards: pd.DataFrame, f2_columns: list[str]) -> pd.DataFrame:
    """List + persistence and rank mean of each F2 column with the list (within-day ranks)."""
    cards = cards.copy()
    listed = day_rank(cards, "static_list")
    cards["list+persistence"] = 0.5 * (listed + day_rank(cards, "persistence"))
    for col in f2_columns:
        name = "rank_mean F2+list" if col == "F2" else f"rank_mean {col}+list"
        cards[name] = 0.5 * (day_rank(cards, col) + listed)
    return cards


def weighted_median(values: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Lower weighted median per row of ``weights`` (values sorted ascending)."""
    cum = np.cumsum(weights, axis=1)
    total = cum[:, -1]
    idx = (cum >= 0.5 * total[:, None]).argmax(axis=1)
    out = values[idx].astype(float)
    out[total <= 0] = np.nan
    return out


def lead_stats(lead: np.ndarray) -> dict:
    if not len(lead):
        return {"captured": 0}
    return {
        "captured": len(lead),
        "median": float(weighted_median(np.sort(lead), np.ones((1, len(lead))))[0]),
        "p25": float(np.percentile(lead, 25)),
        "p75": float(np.percentile(lead, 75)),
        "share_le_24h": float((lead <= 24).mean()),
        "share_le_72h": float((lead <= 72).mean()),
    }


def lead_bootstrap(
    cards,
    links,
    events,
    masks: dict,
    pairs,
    *,
    replicates=400,
    seed=17,
    schemes=("iso_week", "object_id"),
) -> dict:
    """Median lead of captured events per mask and paired block-bootstrap intervals of
    its difference (the same block weights for every mask)."""
    captured = {}
    for name, mask in masks.items():
        o = ev.event_outcomes(cards, links, events, mask)
        o = o[o["evaluable"] & o["captured"]].sort_values("lead_hours").reset_index(drop=True)
        captured[name] = o
    out = {
        "stats": {n: lead_stats(o["lead_hours"].to_numpy(dtype=float)) for n, o in captured.items()}
    }
    out["schemes"] = {}
    for offset, scheme in enumerate(schemes):
        keys = {
            n: ev._block_keys(o["event_start"], o["object_id"], scheme) for n, o in captured.items()
        }
        codes, uniques = pd.factorize(pd.concat(list(keys.values()), ignore_index=True))
        rng = np.random.default_rng(seed + offset)
        draws = rng.integers(0, len(uniques), size=(replicates, len(uniques)))
        weights = np.stack([np.bincount(d, minlength=len(uniques)) for d in draws]).astype(float)
        medians, point, pos = {}, {}, 0
        for n, o in captured.items():
            c = codes[pos : pos + len(o)]
            pos += len(o)
            values = o["lead_hours"].to_numpy(dtype=float)
            medians[n] = (
                weighted_median(values, weights[:, c]) if len(o) else np.full(replicates, np.nan)
            )
            point[n] = out["stats"][n].get("median", np.nan)
        label = "object" if scheme == "object_id" else scheme
        out["schemes"][label] = {
            "blocks": len(uniques),
            "deltas": {
                f"{a} - {b}": V5["_interval"](float(point[a] - point[b]), medians[a] - medians[b])
                for a, b in pairs
            },
        }
    return ev._py(out)


def recall_open(cards, links, events, mask: np.ndarray, days: int) -> float | None:
    """Share of evaluable events that start while a selected card of the pair is open:
    before its release (the day after its first event) or its window end. The recall
    of the protocol counts every event in the card's own window, also after release."""
    out = ev.event_outcomes(cards, links, events, mask)
    out = out[out["evaluable"]]
    if not len(out):
        return None
    keys = V5["KEYS"]
    sel = cards.loc[mask, [*keys, "first_event_start", "y_card"]].copy()
    start = pd.to_datetime(sel["first_event_start"]).where(sel["y_card"] == 1)
    sel["release"] = (start.dt.floor("D") + pd.Timedelta(days=1)).fillna(
        pd.to_datetime(sel["issued_at"]) + pd.Timedelta(days=days)
    )
    linked = links[links["candidate_member"].astype(bool)].merge(sel, on=keys, how="inner")
    starts = events[["event_id", "event_start"]].rename(columns={"event_start": "_start"})
    linked = linked.merge(starts, on="event_id")
    open_ids = set(linked.loc[pd.to_datetime(linked["_start"]) < linked["release"], "event_id"])
    return float((out["captured"] & out["event_id"].isin(open_ids)).mean())


def add_open_recall(points: dict, masks: dict, folds, events, days: int) -> None:
    """Adds ``event_recall_open`` per fold and on average to every curve point."""
    sizes = [len(c) for c, _ in folds]
    bounds = np.cumsum([0, *sizes])
    for k, by_score in masks.items():
        for score, mask in by_score.items():
            entry = points[score][str(k)]
            values = []
            for i, (cards, links) in enumerate(folds):
                part = mask[bounds[i] : bounds[i + 1]]
                value = recall_open(cards, links, events, part, days)
                entry["folds"][i]["event_recall_open"] = value
                values.append(value)
            if all(v is not None for v in values):
                entry["mean"]["event_recall_open"] = float(np.mean(values))


# -- report ----------------------------------------------------------------------------


def load_saved(horizon_label: str, f2_columns: list[str]) -> list:
    folds = []
    for i in range(2):
        cards = pd.read_parquet(OUT / horizon_label / f"cards_fold{i}.parquet")
        links = pd.read_parquet(OUT / horizon_label / f"links_fold{i}.parquet")
        folds.append((add_scores(cards, f2_columns), links))
    return folds


def seed_summary(points: dict, seeds: list[int], budgets) -> dict:
    out = {}
    for group, pattern in SEED_GROUPS.items():
        names = [pattern.format(seed=s) for s in seeds]
        out[group] = {}
        for k in budgets:
            k = str(k)
            block = {}
            for m in (
                "card_precision",
                "event_recall",
                "event_recall_open",
                "lead_hours_median",
                "new_cards_per_day",
            ):
                vals = [points[n][k]["mean"].get(m) for n in names]
                if any(v is None for v in vals):
                    continue
                fold_vals = [[points[n][k]["folds"][i][m] for n in names] for i in range(2)]
                block[m] = {
                    "mean": float(np.mean(vals)),
                    "min": float(np.min(vals)),
                    "max": float(np.max(vals)),
                    "spread": float(np.max(vals) - np.min(vals)),
                    "folds_mean": [float(np.mean(v)) for v in fold_vals],
                }
            out[group][k] = block
    return out


def seven_days(con, config: dict, replicates: int) -> dict:
    events, _ = V5["events_of"](con, TARGET)
    models = model_columns(config)
    seeds = f2_seeds(config)
    f2 = [f"F2 s{s}" for s in seeds]
    folds = load_saved(f"{HORIZON}h", f2)
    budgets = config_budgets(config)
    scores = [
        "static_list",
        "persistence",
        "list+persistence",
        *models,
        *(f"rank_mean {c}+list" for c in f2),
    ]
    points, masks = X["curve"](folds, events, scores, DAYS, budgets)
    add_open_recall(points, masks, folds, events, DAYS)
    cards = pd.concat([c for c, _ in folds], ignore_index=True)
    links = pd.concat([lk for _, lk in folds], ignore_index=True)
    others = [s for s in scores if s != "static_list"]
    boot, lead = {}, {}
    for k in budgets:
        m = {s: masks[k][s] for s in scores}
        boot[str(k)] = V5["bootstrap_masks"](
            cards,
            links,
            events,
            m,
            [(s, "static_list", f"{s} - static_list") for s in others],
            replicates=replicates,
            seed=17,
        )
        lead[str(k)] = lead_bootstrap(
            cards,
            links,
            events,
            {**m, "ceiling": masks[k]["ceiling"]},
            [(s, "static_list") for s in others],
            replicates=replicates,
        )
    return {
        "budgets": budgets,
        "scores": scores,
        "curve": points,
        "seed_summary": seed_summary(points, seeds, budgets),
        "bootstrap_vs_list": boot,
        "lead_vs_list": lead,
    }


def grid(con, persistence_by_year) -> dict:
    """Rules everywhere and models where saved scores exist, K in GRID_K and D."""
    events, members = V5["events_of"](con, TARGET)
    out = {}
    for days in GRID_DAYS:
        horizon = days * 24
        budgets = sorted({*GRID_K, days})
        if days in SAVED:
            f2 = ["F2 s17"] if days == DAYS else ["F2"]
            folds = load_saved(SAVED[days], f2)
            if days == DAYS:
                rename = {
                    "F2 s17": "F2",
                    "F2+sel s17": "F2+sel",
                    "rank_mean F2 s17+list": "rank_mean F2+list",
                }
                folds = [(c.rename(columns=rename), lk) for c, lk in folds]
            models = ["F2", "F2+sel", "rank_mean F2+list"]
        else:
            folds = []
            for year in (2023, 2024):
                cards, links = P2["cards_links"](con, horizon, TARGET, year)
                cards = ev.attach_card_scores(cards, persistence_by_year[year], ["persistence"])
                cards["static_list"] = ev.static_list_scores(cards, members, RULE).to_numpy()
                folds.append((add_scores(cards, []), links))
            models = []
        scores = ["static_list", "persistence", "list+persistence", *models]
        points, masks = X["curve"](folds, events, scores, days, budgets)
        add_open_recall(points, masks, folds, events, days)
        out[f"{days}d"] = {"scores": scores, "models": models, "curve": points}
        print(f"grid {days}d done", flush=True)
    return out


RECALLS = {"as_defined": "event_recall", "open_card": "event_recall_open"}
SHOWN = ("card_precision", "event_recall", "event_recall_open", "new_cards_per_day")


def worst_fold_gap(v: dict, recall: str = "event_recall") -> tuple[float, float, float, float]:
    folds = v["folds"]
    p = min(f["card_precision"] for f in folds)
    r = min(f[recall] for f in folds)
    gap = hypot(max(0.0, GOAL["card_precision"] - p), max(0.0, GOAL["event_recall"] - r))
    f1 = 2 * p * r / (p + r) if p + r > 0 else 0.0
    return gap, p, r, f1


def best_by_worst_fold(points: dict, budgets=None, recall: str = "event_recall") -> dict | None:
    """The point nearest to the goal by the worse fold (ties: larger worse-fold F1)."""
    rows = []
    for score, by_k in points.items():
        if score == "ceiling":
            continue
        for k, v in by_k.items():
            if budgets is not None and int(k) not in budgets:
                continue
            if any(f["card_precision"] is None or f.get(recall) is None for f in v["folds"]):
                continue
            gap, p, r, f1 = worst_fold_gap(v, recall)
            rows.append((gap, -f1, score, int(k), v))
    if not rows:
        return None
    gap, _, score, k, v = min(rows, key=lambda row: (row[0], row[1]))
    return {
        "score": score,
        "k": k,
        "recall": recall,
        "gap_worst_fold": gap,
        "folds": [{m: f.get(m) for m in SHOWN} for f in v["folds"]],
        "mean": {m: v["mean"].get(m) for m in SHOWN},
    }


def goal_every_fold(points: dict, recall: str) -> list[dict]:
    out = []
    for score, by_k in points.items():
        if score == "ceiling":
            continue
        for k, v in by_k.items():
            gap, p, r, _ = worst_fold_gap(v, recall)
            if gap == 0:
                out.append({"score": score, "k": int(k), "min_precision": p, "min_recall": r})
    return out


def summary(grid_result: dict) -> dict:
    out = {}
    for label, recall in RECALLS.items():
        per_horizon, per_cell, every_fold = {}, {}, {}
        for name, block in grid_result.items():
            curve = block["curve"]
            rules = {s: curve[s] for s in ("static_list", "persistence", "list+persistence")}
            per_horizon[name] = {
                "rules": best_by_worst_fold(rules, recall=recall),
                "all": best_by_worst_fold(curve, recall=recall),
            }
            per_cell[name] = {
                k: best_by_worst_fold(curve, {int(k)}, recall) for k in curve["static_list"]
            }
            every_fold[name] = goal_every_fold(curve, recall)
        out[label] = {
            "recall": recall,
            "best_per_horizon": per_horizon,
            "best_per_horizon_k": per_cell,
            "goal_in_every_fold": every_fold,
        }
    return out


def log_reference_runs(config: dict, seven: dict) -> None:
    import mlflow  # noqa: PLC0415

    mlflow.set_tracking_uri(config["mlflow"]["tracking_uri"])
    mlflow.set_experiment(config["mlflow"]["experiment"])
    for k in seven["budgets"]:
        for key, (name, note) in V5["REFERENCE_RUNS"].items():
            metrics = seven["curve"][key][str(k)]["mean"]
            with mlflow.start_run(run_name=f"{name} ge2s 168h release_open{k} (разведка 3)"):
                mlflow.set_tags(
                    {
                        "kind": "reference",
                        "reference": key,
                        "is_model": "false",
                        "tuning_version": config["version"],
                        "mlflow.note.content": note,
                    }
                )
                mlflow.log_metrics({m: float(v) for m, v in metrics.items() if v is not None})


def report(config: dict, replicates: int) -> dict:
    started = perf_counter()
    con = V5["_con"]()
    fits = [json.loads((DIR / f"fits_fold{i}.json").read_text(encoding="utf-8")) for i in range(2)]
    seven = seven_days(con, config, replicates)
    print(f"7d done ({round(perf_counter() - started)} s)", flush=True)
    persistence_by_year = {
        y: V5["channel_persistence"](con, V5["year_months"](y)) for y in (2023, 2024)
    }
    grid_result = grid(con, persistence_by_year)
    return {
        "stage": "exploration 3 — 7 days (8 fits; rules and saved scores elsewhere)",
        "note": X["NOTE"],
        "config": str(CONFIG.relative_to(ROOT)),
        "policy": "rolling list with release, at most K open (window = horizon)",
        "recall_definitions": {
            "event_recall": "protocol since stage 0: an event is captured if it starts in the "
            "own window of a selected card, also after the card was released",
            "event_recall_open": "added in exploration 3: an event is captured only if it "
            "starts while a selected card of the pair is open (before its release or window end)",
        },
        "fits": [
            {
                k: f[k]
                for k in ("fold", "set", "seed", "seconds", "fit_rows", "fit_positive", "columns")
            }
            for block in fits
            for f in block["fits"]
        ],
        "fit_fold_seconds": [block["seconds"] for block in fits],
        "hygiene": fits[0]["hygiene"],
        "seven_days": seven,
        "grid": grid_result,
        "summary": summary(grid_result),
        "seconds": round(perf_counter() - started, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["fit", "report"], required=True)
    parser.add_argument("--fold", type=int, choices=[0, 1])
    parser.add_argument("--replicates", type=int, default=400)
    parser.add_argument("--no-mlflow", action="store_true")
    args = parser.parse_args()
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    if args.stage == "fit":
        if args.fold is None:
            raise SystemExit("--fold is required for --stage fit")
        fit_fold(config, args.fold, not args.no_mlflow)
        return
    result = {**report(config, args.replicates), **V5["_provenance"]()}
    V5["_write"](REPORT, result)
    if not args.no_mlflow:
        log_reference_runs(config, result["seven_days"])
    print(f"report: {REPORT} ({result['seconds']} s)")


if __name__ == "__main__":
    main()

"""``sensor-failure-tuning-v5`` runner (plan frozen by the team's instruction, amended
before any fit). Development folds only (evaluation years 2023 and 2024); calibration
and holdout are never read; there is no final stage.

- ``stage0`` — no fitting: thresholds {> 2 s, >= 2 s, all} × horizons 5/7/10/14 days ×
  budgets {D open, 5 open} × {with release, without}: events, base rate, ceiling,
  persistence and static list P/R, median lead, new cards per day and per window.
- ``fit`` — the 10 pre-registered fits (target >= 2 s, seed 17, CatBoost c3, 6 threads)
  for one horizon: both folds, every fit an MLflow run with its model; card scores go
  to gitignored ``artifacts/sensor-failure-v1/tuning_v5/<H>h/``.
- ``report`` — models vs static list and persistence (paired week-block bootstrap of
  the main policy), exploratory set differences, references; reference runs in MLflow
  with explicit names. Aggregates only.

    python data-science/scripts/tune_sensor_failure_v5.py --stage stage0
    python data-science/scripts/tune_sensor_failure_v5.py --stage fit --horizon 240
    python data-science/scripts/tune_sensor_failure_v5.py --stage fit --horizon 336
    python data-science/scripts/tune_sensor_failure_v5.py --stage report
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from time import perf_counter

import duckdb
import numpy as np
import pandas as pd

from infra_pulse_research.modeling import sensor_failure_evaluation as ev
from infra_pulse_research.modeling import sensor_failure_fe_v4 as fe
from infra_pulse_research.modeling.sensor_failure_modeling import (
    negative_sample_mask,
    persistence_score,
)
from infra_pulse_research.modeling.sensor_failure_nodes import (
    ceiling_metrics,
    policy_metrics,
    select_v4,
)
from infra_pulse_research.modeling.subsystem_evaluation import _interval

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "data-science/configs/sensor_failure_tuning_v5.json"
SF = ROOT / "data-science/artifacts/sensor-failure-v1"
LABELS = SF / "model_labels_v5"
F2 = SF / "features"
V4 = SF / "features_v4"
OUT = SF / "tuning_v5"
STAGE0_REPORT = ROOT / "data-science/reports/sensor-failure-v5-stage0-2026-09-26.json"
CV_REPORT = ROOT / "data-science/reports/sensor-failure-tuning-v5-cv-2026-09-26.json"
DEV_END = "2025-01-01"
DEV_MONTHS = [f"{y}-{m:02d}" for y in (2019, 2020, 2022, 2023, 2024) for m in range(1, 13)]
HORIZONS = {120: 5, 168: 7, 240: 10, 336: 14}
THRESHOLDS = {
    "gt2s": ("connection_loss_gt2s", {"op": ">", "seconds": 2}),
    "ge2s": ("connection_loss_ge2s", {"op": ">=", "seconds": 2}),
    "all": ("connection_loss_all", None),
}
LABEL = "connection_loss"
KEYS = ["object_id", "sensor_type", "issued_at"]
GROUP_OF_PREFIX = {fe.PREFIX[g]: g for g in fe.GROUPS}
REFERENCES = ("persistence", "static_list")
REFERENCE_RUNS = {
    "ceiling": (
        "ORACLE ceiling (не модель)",
        "Потолок: жадный оракул, знающий будущие события; верхняя граница, не модель.",
    ),
    "persistence": (
        "persistence (правило)",
        "Правило без обучения: давность последнего кандидата метки на канале карточки.",
    ),
    "static_list": (
        "static list (правило)",
        "Правило без обучения: число событий пары объект × тип за прошлые 365 сут.",
    ),
}


def policies(days: int) -> dict[str, dict]:
    """Main (release, D open), extra (release, 5 open) and references without release."""
    return {
        f"release_open{days}": {"type": "rolling_release", "max_open": days, "window_days": days},
        "release_open5": {"type": "rolling_release", "max_open": 5, "window_days": days},
        f"norelease_open{days}": {"type": "rolling", "max_open": days, "window_days": days},
        "norelease_open5": {"type": "rolling", "max_open": 5, "window_days": days},
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


def _con() -> duckdb.DuckDBPyConnection:
    """DuckDB with its own spill directory: parallel processes must not share the
    default ``.tmp`` of the working directory (they overwrite each other's file)."""
    tmp = OUT / "tmp" / str(os.getpid())
    tmp.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET threads=6; SET memory_limit='6GB'; SET temp_directory={_q(tmp)}")
    return con


def events_of(con, target: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    root = LABELS / "events" / target
    events = con.execute(
        f"SELECT * FROM read_parquet({_q(root / 'events.parquet')}) "
        f"WHERE event_start < TIMESTAMP {_q(DEV_END)}"
    ).df()
    members = con.execute(
        f"SELECT * FROM read_parquet({_q(root / 'event_members.parquet')}) "
        f"WHERE start_at < TIMESTAMP {_q(DEV_END)}"
    ).df()
    return events, members


def cards_of(con, target: str, horizon: int, months) -> tuple[pd.DataFrame, pd.DataFrame]:
    out = []
    for kind in ("cards", "links"):
        files = _files(LABELS / f"{kind}/{target}/{horizon}h/{m}.parquet" for m in months)
        out.append(
            con.execute(
                f"SELECT * FROM read_parquet({files}) WHERE issued_at < TIMESTAMP {_q(DEV_END)}"
            ).df()
        )
    return out[0].reset_index(drop=True), out[1]


def channel_persistence(con, months) -> pd.DataFrame:
    """Candidate channels (identical for every threshold and horizon) with persistence."""
    labels = _files(LABELS / f"channels/connection_loss_all/{m}.parquet" for m in months)
    f2 = _files(F2 / f"month={m}/features.parquet" for m in months)
    frame = con.execute(f"""
        SELECT l.channel_id, l.object_id, l.sensor_type, l.issued_at, l.candidate,
               f.hist__{LABEL}__hours_since_last_candidate, f.hist__{LABEL}__starts_30d
        FROM read_parquet({labels}) l JOIN read_parquet({f2}) f USING (channel_id, issued_at)
        WHERE l.candidate AND l.issued_at < TIMESTAMP {_q(DEV_END)}
    """).df()
    frame["persistence"] = persistence_score(frame, LABEL)
    return frame


def bootstrap_masks(
    cards: pd.DataFrame,
    links: pd.DataFrame,
    events: pd.DataFrame,
    masks: dict[str, np.ndarray],
    deltas,
    *,
    replicates: int = 400,
    seed: int = 17,
    schemes=("iso_week", "object_id"),
) -> dict:
    """Block bootstrap of card precision and event recall for fixed issue masks (paired:
    the same replicate weights for every mask), as ``bootstrap_intervals`` does for its
    own selections; used for policies that it does not know (rolling with release)."""
    evaluable = cards["outcome"].isin(ev.EVALUABLE).to_numpy()
    rows = cards[evaluable].reset_index(drop=True)
    y = rows["y_card"].to_numpy(dtype=float)
    sel = {k: m[evaluable].astype(float) for k, m in masks.items()}
    outcomes = {k: ev.event_outcomes(cards, links, events, m) for k, m in masks.items()}
    first = next(iter(outcomes.values()))
    base = first[first["evaluable"]].reset_index(drop=True)
    captured = {
        k: base[["event_id"]]
        .merge(o[["event_id", "captured"]], on="event_id", how="left")["captured"]
        .fillna(False)
        .to_numpy(dtype=float)
        for k, o in outcomes.items()
    }

    def metrics(wc, we):
        out = {}
        for k in masks:
            with np.errstate(invalid="ignore", divide="ignore"):
                n = wc @ sel[k]
                out[k] = {
                    "card_precision": np.where(n > 0, (wc @ (sel[k] * y)) / n, np.nan),
                    "event_recall": np.where(
                        we.sum(axis=1) > 0, (we @ captured[k]) / we.sum(axis=1), np.nan
                    ),
                }
        return out

    point = metrics(np.ones((1, len(rows))), np.ones((1, len(base))))
    result = {"replicates": replicates, "seed": seed, "schemes": {}}
    for offset, scheme in enumerate(schemes):
        ck = ev._block_keys(rows["issued_at"], rows["object_id"], scheme)
        ek = ev._block_keys(base["event_start"], base["object_id"], scheme)
        codes, uniques = pd.factorize(pd.concat([ck, ek], ignore_index=True))
        cc, ec = codes[: len(rows)], codes[len(rows) :]
        rng = np.random.default_rng(seed + offset)
        draws = rng.integers(0, len(uniques), size=(replicates, len(uniques)))
        counts = np.stack([np.bincount(d, minlength=len(uniques)) for d in draws]).astype(float)
        values = metrics(counts[:, cc], counts[:, ec])
        block = {
            "blocks": len(uniques),
            "scores": {
                k: {m: _interval(float(point[k][m][0]), values[k][m]) for m in values[k]}
                for k in masks
            },
            "deltas": {},
        }
        for a, b, name in deltas:
            block["deltas"][name] = {}
            for m in values[a]:
                diff = values[a][m] - values[b][m]
                entry = _interval(float(point[a][m][0] - point[b][m][0]), diff)
                valid = diff[np.isfinite(diff)]
                entry["share_positive"] = float((valid > 0).mean()) if len(valid) else None
                block["deltas"][name][m] = entry
        result["schemes"]["object" if scheme == "object_id" else scheme] = block
    return ev._py(result)


# -- stage 0 ---------------------------------------------------------------------------


def stage0() -> dict:
    started = perf_counter()
    con = _con()
    per_year: dict = {}
    for year in (2023, 2024):
        months = year_months(year)
        chan = channel_persistence(con, months)
        block = {}
        for tname, (target, rule) in THRESHOLDS.items():
            events, members = events_of(con, target)
            for horizon, days in HORIZONS.items():
                cards, links = cards_of(con, target, horizon, months)
                cards = ev.attach_card_scores(cards, chan, ["persistence"])
                cards["static_list"] = ev.static_list_scores(cards, members, rule).to_numpy()
                entry = {}
                for pname, policy in policies(days).items():
                    entry[pname] = {
                        s: policy_metrics(cards, links, events, s, policy) for s in REFERENCES
                    }
                    entry[pname]["ceiling"] = ceiling_metrics(cards, links, events, policy)
                block[f"{tname} {horizon}h"] = entry
        per_year[str(year)] = block
        print(f"stage0 {year} done ({round(perf_counter() - started)} s)", flush=True)
    keys = (
        "card_precision",
        "event_recall",
        "lead_hours_median",
        "new_cards_per_day",
        "events",
        "card_base_rate",
        "cards_evaluable",
    )
    mean: dict = {}
    for name, entry in per_year["2023"].items():
        days = HORIZONS[int(name.split()[1].removesuffix("h"))]
        mean[name] = {}
        for pname, scores in entry.items():
            mean[name][pname] = {}
            for score in scores:
                vals = {
                    k: float(np.mean([per_year[y][name][pname][score][k] for y in per_year]))
                    for k in keys
                    if all(per_year[y][name][pname][score].get(k) is not None for y in per_year)
                }
                vals["new_cards_per_window"] = vals.get("new_cards_per_day", 0.0) * days
                mean[name][pname][score] = vals
    return {
        "stage": "stage0 (no fitting; evaluation years 2023/2024; calibration/holdout not read)",
        "thresholds": {k: {"target": t, "rule": r} for k, (t, r) in THRESHOLDS.items()},
        "horizon_days": {f"{h}h": d for h, d in HORIZONS.items()},
        "per_year": per_year,
        "mean_2023_2024": mean,
        "seconds": round(perf_counter() - started, 1),
    }


# -- stage 1 fits ----------------------------------------------------------------------


def set_columns(frame: pd.DataFrame, prefixes) -> list[str]:
    cols = sorted(c for c in frame.columns if c.startswith(tuple(prefixes)))
    if any(c.startswith("num__") for c in cols):
        raise ValueError("feature sets must not contain value_numeric features")
    return cols


def _feature_join(groups, months) -> str:
    joins = [
        f"JOIN read_parquet({_files(F2 / f'month={m}/features.parquet' for m in months)}) f2 "
        "USING (channel_id, issued_at)"
    ]
    for g in groups:
        files = _files(V4 / g / f"month={m}/features.parquet" for m in months)
        joins.append(f"JOIN read_parquet({files}) g_{g} USING (channel_id, issued_at)")
    return " ".join(joins)


def fit_horizon(config: dict, horizon: int, mlflow_on: bool) -> dict:
    from infra_pulse_research.modeling.sensor_failure_tuning import (  # noqa: PLC0415
        FittedCatBoost,
        card_aggregates,
    )

    started = perf_counter()
    stage = config["stage1"]
    target = stage["target"]
    rule = config["thresholds"]["ge2s"]["episode_filter"]
    sets = [f["set"] for f in stage["fits"] if f["horizon"] == f"{horizon}h"]
    days = HORIZONS[horizon]
    cb = {k: v for k, v in stage["catboost"].items() if k != "config"}
    groups = sorted(
        {GROUP_OF_PREFIX[p] for s in sets for p in stage["feature_sets"][s] if p in GROUP_OF_PREFIX}
    )
    con = _con()
    events, members = events_of(con, target)
    mlflow = None
    if mlflow_on:
        import mlflow  # noqa: PLC0415

        mlflow.set_tracking_uri(config["mlflow"]["tracking_uri"])
        mlflow.set_experiment(config["mlflow"]["experiment"])
    out_dir = OUT / f"{horizon}h"
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"horizon": horizon, "target": target, "sets": sets, "fits": [], "folds": {}}
    channels_files = _files(LABELS / f"channels/{target}/{m}.parquet" for m in DEV_MONTHS)
    for index, fold in enumerate(config["folds"]):
        t0 = perf_counter()
        ranges = " OR ".join(
            f"(issued_at >= TIMESTAMP {_q(a)} AND issued_at < TIMESTAMP {_q(b)} - "
            f"INTERVAL {horizon} HOUR)"
            for a, b in fold["fit"]
        )
        keys = con.execute(f"""
            SELECT channel_id, issued_at, y_{horizon}h::INT AS y FROM read_parquet({channels_files})
            WHERE split_period = 'development' AND candidate
              AND outcome_{horizon}h IN ('positive', 'negative') AND ({ranges})
        """).df()
        keep = (keys["y"] == 1).to_numpy() | negative_sample_mask(keys.channel_id, keys.issued_at)
        con.register("chosen", keys.loc[keep])
        sample = con.execute(
            f"SELECT chosen.y, * EXCLUDE (y) FROM chosen {_feature_join(groups, DEV_MONTHS)} "
            "ORDER BY channel_id, issued_at"
        ).df()
        con.unregister("chosen")
        if len(sample) != int(keep.sum()):
            raise ValueError("features missing for fit rows")
        y = sample.pop("y").to_numpy(dtype=int)
        fitted, runs = {}, {}
        for name in sets:
            columns = set_columns(sample, stage["feature_sets"][name])
            t1 = perf_counter()
            fitted[name] = FittedCatBoost(sample, y, columns, {}, cb)
            info = {
                "horizon": horizon,
                "fold": index,
                "set": name,
                "seed": cb["random_seed"],
                "thread_count": cb["thread_count"],
                "fit_rows": len(sample),
                "fit_positive": int(y.sum()),
                "columns": len(columns),
                "seconds": round(perf_counter() - t1, 1),
                "top_features": fitted[name].top_features,
            }
            result["fits"].append(info)
            print(f"{horizon}h fold{index} {name}: {info['seconds']} s", flush=True)
            if mlflow is not None:
                with mlflow.start_run(
                    run_name=f"fit ge2s {horizon}h fold{index} {name} s17"
                ) as run:
                    mlflow.set_tags(
                        {
                            "kind": "fit",
                            "is_model": "true",
                            "tuning_version": config["version"],
                            "target": target,
                            "horizon": f"{horizon}h",
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
        months = year_months(int(fold["evaluate"][0][0][:4]))
        parts = []
        for month in months:
            lab = _q(LABELS / f"channels/{target}/{month}.parquet")
            extra = "".join(f", g_{g}.* EXCLUDE (channel_id, issued_at)" for g in groups)
            frame = con.execute(f"""
                SELECT l.channel_id, l.issued_at, l.object_id, l.sensor_type, l.candidate,
                       f2.* EXCLUDE (channel_id, issued_at, object_id, sensor_type){extra}
                FROM read_parquet({lab}) l {_feature_join(groups, [month])}
                WHERE l.candidate AND l.issued_at < TIMESTAMP {_q(DEV_END)}
            """).df()
            ch = frame[["channel_id", "issued_at", "object_id", "sensor_type", "candidate"]].copy()
            ch["persistence"] = persistence_score(frame, LABEL)
            for name, model in fitted.items():
                ch[name] = model.score(frame)
            parts.append(ch)
        channels = pd.concat(parts, ignore_index=True)
        cards, links = cards_of(con, target, horizon, months)
        table = cards.copy()
        for col, how in [("persistence", "max"), *((s, "mean_top2") for s in sets)]:
            agg = card_aggregates(channels, col, how).rename(columns={"score": col})
            table = table.merge(agg, on=KEYS, how="left")
        if len(table) != len(cards):
            raise ValueError("card merge changed rows")
        table["static_list"] = ev.static_list_scores(table, members, rule).to_numpy()
        table.to_parquet(out_dir / f"cards_fold{index}.parquet", index=False)
        links.to_parquet(out_dir / f"links_fold{index}.parquet", index=False)
        fold_result = {}
        for pname, policy in policies(days).items():
            block = {
                s: policy_metrics(table, links, events, s, policy) for s in [*sets, *REFERENCES]
            }
            block["ceiling"] = ceiling_metrics(table, links, events, policy)
            fold_result[pname] = block
        result["folds"][str(index)] = fold_result
        if mlflow is not None:
            for name, run_id in runs.items():
                with mlflow.start_run(run_id=run_id):
                    for pname, block in fold_result.items():
                        mlflow.log_metrics(
                            {
                                f"{pname}_{k}": float(v)
                                for k, v in block[name].items()
                                if isinstance(v, int | float) and v is not None
                            }
                        )
        print(f"{horizon}h fold{index} done in {round(perf_counter() - t0)} s", flush=True)
    result["seconds"] = round(perf_counter() - started, 1)
    result["hygiene"] = {
        "thread_count": cb["thread_count"],
        "os_cpu_count": os.cpu_count(),
        "cpu_model": subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True
        ).stdout.strip(),
    }
    (out_dir / "fits.json").write_text(
        json.dumps(ev._py(result), ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return result


# -- report ----------------------------------------------------------------------------


def report(config: dict, replicates: int) -> dict:
    started = perf_counter()
    target = config["stage1"]["target"]
    con = _con()
    events, _ = events_of(con, target)
    out = {
        "stage": "stage1 report (target >= 2 s, seed 17, folds 2023/2024)",
        "status": config["status"],
        "amended_before_fit": config["amended_before_fit"],
        "noise_reference": "v3: seed spread of fold-mean precision up to 0.014 at about 0.15 "
        "(24 h, 2 per day); one seed here gives no noise estimate",
        "horizons": {},
    }
    for horizon in (240, 336):
        path = OUT / f"{horizon}h" / "fits.json"
        if not path.exists():
            continue
        fits = json.loads(path.read_text(encoding="utf-8"))
        days = HORIZONS[horizon]
        sets = fits["sets"]
        cards = pd.concat(
            [pd.read_parquet(OUT / f"{horizon}h" / f"cards_fold{i}.parquet") for i in range(2)],
            ignore_index=True,
        )
        links = pd.concat(
            [pd.read_parquet(OUT / f"{horizon}h" / f"links_fold{i}.parquet") for i in range(2)],
            ignore_index=True,
        )
        block = {"sets": sets, "policies": {}}
        for pname, policy in policies(days).items():
            per_fold = {
                s: [fits["folds"][str(i)][pname][s] for i in range(2)]
                for s in [*sets, *REFERENCES, "ceiling"]
            }
            summary = {
                s: {
                    "folds": [
                        {
                            k: v[k]
                            for k in (
                                "card_precision",
                                "event_recall",
                                "lead_hours_median",
                                "new_cards_per_day",
                                "cards_selected",
                            )
                        }
                        for v in vals
                    ],
                    "mean": {
                        k: float(np.mean([v[k] for v in vals]))
                        for k in (
                            "card_precision",
                            "event_recall",
                            "lead_hours_median",
                            "new_cards_per_day",
                        )
                        if all(v[k] is not None for v in vals)
                    },
                }
                for s, vals in per_fold.items()
            }
            entry = {"summary": summary}
            entry["exploration_vs_F2"] = {
                s: [
                    per_fold[s][i]["card_precision"] - per_fold["F2"][i]["card_precision"]
                    for i in range(2)
                ]
                for s in sets
                if s != "F2"
            }
            masks = {s: select_v4(cards, s, policy) for s in [*sets, *REFERENCES]}
            deltas = [(s, r, f"{s} - {r}") for s in sets for r in REFERENCES]
            deltas += [(s, "F2", f"{s} - F2") for s in sets if s != "F2"]
            entry["bootstrap"] = bootstrap_masks(
                cards, links, events, masks, deltas, replicates=replicates, seed=17
            )
            if pname == f"release_open{days}":
                week = entry["bootstrap"]["schemes"]["iso_week"]
                verdict = {}
                for s in sets:
                    delta = week["deltas"][f"{s} - static_list"]["card_precision"]
                    rec_m = week["scores"][s]["event_recall"]["point"]
                    rec_s = week["scores"]["static_list"]["event_recall"]["point"]
                    verdict[s] = {
                        "precision_minus_static_week": delta,
                        "precision_minus_static_object": entry["bootstrap"]["schemes"]["object"][
                            "deltas"
                        ][f"{s} - static_list"]["card_precision"],
                        "precision_minus_persistence_week": week["deltas"][f"{s} - persistence"][
                            "card_precision"
                        ],
                        "recall_model": rec_m,
                        "recall_static_list": rec_s,
                        "pass": bool(delta["low"] > 0 and rec_m >= rec_s - 0.02),
                    }
                entry["criterion_main"] = verdict
            block["policies"][pname] = entry
        block["fits"] = [
            {k: f[k] for k in ("fold", "set", "seconds", "fit_rows", "fit_positive", "columns")}
            for f in fits["fits"]
        ]
        block["hygiene"] = fits["hygiene"]
        out["horizons"][f"{horizon}h"] = block
    out["seconds"] = round(perf_counter() - started, 1)
    return out


def log_reference_runs(config: dict, result: dict) -> None:
    import mlflow  # noqa: PLC0415

    mlflow.set_tracking_uri(config["mlflow"]["tracking_uri"])
    mlflow.set_experiment(config["mlflow"]["experiment"])
    for horizon, block in result["horizons"].items():
        for pname, entry in block["policies"].items():
            for key, (name, note) in REFERENCE_RUNS.items():
                metrics = entry["summary"][key]["mean"]
                with mlflow.start_run(run_name=f"{name} ge2s {horizon} {pname}"):
                    mlflow.set_tags(
                        {
                            "kind": "reference",
                            "reference": key,
                            "is_model": "false",
                            "tuning_version": config["version"],
                            "mlflow.note.content": note,
                        }
                    )
                    mlflow.log_metrics({k: float(v) for k, v in metrics.items()})


def _provenance() -> dict:
    return {
        "config_sha256": _sha(CONFIG),
        "labels_summary_sha256": _sha(LABELS / "summary.json"),
        "git_sha": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
        ).stdout.strip(),
        "git_dirty": bool(
            subprocess.run(
                ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True
            ).stdout.strip()
        ),
    }


def _write(path: Path, result: dict) -> None:
    text = json.dumps(ev._py(result), ensure_ascii=False, indent=1) + "\n"
    if any(k in text for k in ('"object_id"', '"channel_id"', '"event_id"', '"node_id"')):
        raise ValueError("report must not contain identifiers")
    path.write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["stage0", "fit", "report"], required=True)
    parser.add_argument("--horizon", type=int, choices=[240, 336])
    parser.add_argument("--replicates", type=int, default=400)
    parser.add_argument("--no-mlflow", action="store_true")
    args = parser.parse_args()
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    if not config["status"].startswith("frozen_before_fit"):
        raise SystemExit("v5 plan is not frozen")
    if not args.no_mlflow:
        from mlflow.tracking import MlflowClient  # noqa: PLC0415

        client = MlflowClient(config["mlflow"]["tracking_uri"])
        if client.get_experiment_by_name(config["mlflow"]["experiment"]) is None:
            client.create_experiment(config["mlflow"]["experiment"])
    if args.stage == "stage0":
        result = {**stage0(), **_provenance()}
        _write(STAGE0_REPORT, result)
        OUT.mkdir(parents=True, exist_ok=True)
        _write(OUT / "stage0.json", result)
        print(f"report: {STAGE0_REPORT} ({result['seconds']} s)")
    elif args.stage == "fit":
        if args.horizon is None:
            raise SystemExit("--horizon is required for --stage fit")
        fit_horizon(config, args.horizon, not args.no_mlflow)
    else:
        result = {**report(config, args.replicates), **_provenance()}
        _write(CV_REPORT, result)
        if not args.no_mlflow:
            log_reference_runs(config, result)
        print(f"report: {CV_REPORT} ({result['seconds']} s)")


if __name__ == "__main__":
    main()

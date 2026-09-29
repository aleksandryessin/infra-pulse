"""Build sensor-failure-v1 candidates/episodes/clusters and measure them on development.

``--model-labels`` instead builds the frozen model-protocol tables for all months
2019-01..2026-06: channel labels, cards, event links and events per target, and a
count-only QA summary (rows/positives per split; no holdout metrics).

Development only (cutoffs/starts in 2019-01-31..2020-12-31 and 2022-01-31..2024-12-31).
Local tables go to data-science/artifacts/sensor-failure-v1/ (Git-ignored); the
aggregated report has no object/channel identifiers.

    uv run --locked --group platform --group train --group research \
        python data-science/scripts/measure_sensor_failure.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import duckdb

from infra_pulse_research.modeling import sensor_failure_target as sft
from infra_pulse_research.modeling.fire_source import open_fire_source
from infra_pulse_research.modeling.subsystem_grid import register_registry_views, study_months

ROOT = Path(__file__).resolve().parents[2]
DEV_MONTHS = [m for m in study_months("2019-01", "2024-12") if not m.startswith("2021")]
MAIN_LABELS = ("technical_value", "connection_loss")


def _q(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _dev(expr: str) -> str:
    """Development interval predicate for a timestamp expression."""
    return (
        f"(({expr} >= TIMESTAMP '2019-01-31' AND {expr} < TIMESTAMP '2021-01-01') OR "
        f"({expr} >= TIMESTAMP '2022-01-31' AND {expr} < TIMESTAMP '2025-01-01'))"
    )


def _records(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    frame = con.sql(sql).df()
    return json.loads(frame.to_json(orient="records", force_ascii=False))


def _one(con: duckdb.DuckDBPyConnection, sql: str) -> dict:
    rows = _records(con, sql)
    return rows[0] if rows else {}


def episodes_report(con, tables: Path, config: dict) -> dict:
    out = {}
    for q in config["episode"]["q_hours_grid"]:
        ep = f"read_parquet({_q(tables / f'episodes_q{q}h.parquet')})"
        dev = f"(SELECT * FROM {ep} WHERE {_dev('start_at')})"
        block = {
            "starts_total": _records(
                con,
                f"""SELECT label, COUNT(*) AS starts, COUNT_IF(q_history_ok) AS starts_q_history_ok,
                       COUNT(DISTINCT channel_id) AS channels, COUNT(DISTINCT object_id) AS objects
                    FROM {dev} GROUP BY label ORDER BY label""",
            ),
        }
        base = f"(SELECT * FROM {dev} WHERE q_history_ok)"
        block["by_year_sensor"] = _records(
            con,
            f"""SELECT label, year(start_at) AS year, sensor_type, COUNT(*) AS starts,
                   COUNT(DISTINCT channel_id) AS channels, COUNT(DISTINCT object_id) AS objects,
                   ROUND(MEDIAN(duration_hours), 3) AS duration_median_h,
                   ROUND(quantile_cont(duration_hours, 0.9), 2) AS duration_p90_h,
                   ROUND(AVG(open_at_data_end::INT), 4) AS open_share,
                   ROUND(AVG((open_at_data_end OR duration_hours >= 1)::INT), 4) AS share_ge_1h,
                   ROUND(AVG((open_at_data_end OR duration_hours >= 24)::INT), 4) AS share_ge_24h,
                   ROUND(AVG(start_alarm_at_ts::INT), 4) AS alarm_at_start_ts_share,
                   ROUND(AVG(start_conflict::INT), 4) AS conflict_at_start_share
                FROM {base} GROUP BY ALL ORDER BY label, year, starts DESC, sensor_type""",
        )
        block["by_label"] = _records(
            con,
            f"""SELECT label, COUNT(*) AS starts,
                   ROUND(MEDIAN(duration_hours), 3) AS duration_median_h,
                   ROUND(quantile_cont(duration_hours, 0.9), 2) AS duration_p90_h,
                   ROUND(AVG((open_at_data_end OR duration_hours >= 1)::INT), 4) AS share_ge_1h,
                   ROUND(AVG((open_at_data_end OR duration_hours >= 24)::INT), 4) AS share_ge_24h,
                   ROUND(AVG(start_alarm_at_ts::INT), 4) AS alarm_at_start_ts_share
                FROM {base} GROUP BY label ORDER BY label""",
        )
        block["recurrence_per_channel_year"] = _records(
            con,
            f"""WITH cy AS (SELECT label, channel_id, year(start_at) y, COUNT(*) n FROM {base}
                            GROUP BY ALL)
                SELECT label, COUNT(*) AS channel_years, MEDIAN(n) AS median_starts,
                       quantile_cont(n, 0.9) AS p90_starts, MAX(n) AS max_starts,
                       ROUND(AVG((n >= 2)::INT), 4) AS share_ge_2,
                       ROUND(AVG((n >= 5)::INT), 4) AS share_ge_5
                FROM cy GROUP BY label ORDER BY label""",
        )
        conc = []
        for unit in ("channel_id", "object_id"):
            conc += _records(
                con,
                f"""WITH u AS (SELECT label, {unit} AS k, COUNT(*) n FROM {base} GROUP BY ALL),
                    r AS (SELECT *, SUM(n) OVER (PARTITION BY label ORDER BY n DESC, k
                                                 ROWS UNBOUNDED PRECEDING) AS cum,
                                 SUM(n) OVER (PARTITION BY label) AS tot,
                                 COUNT(*) OVER (PARTITION BY label) AS units FROM u)
                    SELECT label, '{unit}' AS unit, MAX(units) AS units_with_starts,
                           COUNT_IF(cum - n < tot / 2.0) AS units_for_half_of_starts,
                           ROUND(COUNT_IF(cum - n < tot / 2.0) / MAX(units), 4) AS share_of_units
                    FROM r GROUP BY label ORDER BY label""",
            )
        block["concentration_half_of_starts"] = conc
        clusters = []
        for w in config["cluster"]["w_minutes_grid"]:
            clusters += _records(
                con,
                f"""WITH s AS (SELECT label, cluster_size_w{w} AS size, cluster_id_w{w} AS cid
                               FROM {base})
                    SELECT label, {w} AS w_minutes, COUNT(*) AS starts,
                           COUNT(DISTINCT cid) AS events,
                           ROUND(AVG((size >= 2)::INT), 4) AS share_starts_in_clusters,
                           ROUND(AVG((size BETWEEN 2 AND 4)::INT), 4) AS share_size_2_4,
                           ROUND(AVG((size BETWEEN 5 AND 75)::INT), 4) AS share_size_5_75,
                           ROUND(AVG((size > 75)::INT), 4) AS share_size_gt_75,
                           MAX(size) AS max_size
                    FROM s GROUP BY label ORDER BY label""",
            )
        block["clusters"] = clusters
        block["cluster_size_quantiles"] = []
        for w in config["cluster"]["w_minutes_grid"]:
            path = tables / f"clusters_q{q}h_w{w}m.parquet"
            block["cluster_size_quantiles"] += _records(
                con,
                f"""SELECT label, {w} AS w_minutes, COUNT(*) AS clusters,
                       COUNT_IF(size >= 2) AS multi_channel_clusters,
                       quantile_cont(size, 0.5) AS size_p50,
                       quantile_cont(size, 0.9) AS size_p90,
                       quantile_cont(size, 0.99) AS size_p99,
                       ROUND(AVG((span_minutes > {w})::INT) FILTER (WHERE size >= 2), 4)
                         AS chained_beyond_w_share,
                       ROUND(AVG((sensor_types > 1)::INT) FILTER (WHERE size >= 2), 4)
                         AS multi_sensor_type_share
                    FROM read_parquet({_q(path)}) WHERE {_dev("cluster_start")}
                    GROUP BY label ORDER BY label""",
            )
        out[f"q{q}h"] = block
    return out


def record_level_claim(con, tables: Path) -> list[dict]:
    """Candidate timestamps (not episode starts) chained per object within 10 minutes."""
    cand = f"read_parquet({_q(tables / 'candidates.parquet')})"
    return _records(
        con,
        f"""WITH c AS (SELECT label, object_id, channel_id, event_at FROM {cand}
                       WHERE label LIKE 'connection_loss%' AND {_dev("event_at")}),
            g AS (SELECT *, SUM(CASE WHEN prev IS NULL OR event_at - prev > INTERVAL 10 MINUTE
                                     THEN 1 ELSE 0 END)
                          OVER (PARTITION BY label, object_id ORDER BY event_at, channel_id
                                ROWS UNBOUNDED PRECEDING) AS seq
                  FROM (SELECT *, LAG(event_at) OVER (PARTITION BY label, object_id
                                                      ORDER BY event_at, channel_id) AS prev
                        FROM c)),
            k AS (SELECT label, object_id, seq, COUNT(DISTINCT channel_id) AS channels,
                         COUNT(*) AS timestamps FROM g GROUP BY ALL)
            SELECT label, SUM(timestamps) AS candidate_timestamps,
                   ROUND(SUM(timestamps) FILTER (WHERE channels BETWEEN 5 AND 75)
                         / SUM(timestamps), 4) AS share_in_packs_5_75,
                   ROUND(SUM(timestamps) FILTER (WHERE channels >= 2) / SUM(timestamps), 4)
                     AS share_in_packs_ge_2
            FROM k GROUP BY label ORDER BY label""",
    )


def co_occurrence(con, tables: Path) -> list[dict]:
    cand = f"read_parquet({_q(tables / 'candidates.parquet')})"
    return _records(
        con,
        f"""SELECT label, sensor_type, COUNT(*) AS candidate_timestamps,
               ROUND(AVG((alarm_rows_at_ts > 0)::INT), 4) AS alarm_at_ts_share,
               ROUND(AVG((candidate_alarm_rows > 0)::INT), 4) AS alarm_on_candidate_row_share,
               ROUND(AVG((clean_rows_at_ts > 0)::INT), 4) AS conflict_share
            FROM {cand} WHERE {_dev("event_at")}
            GROUP BY ALL ORDER BY label, candidate_timestamps DESC""",
    )


def labels_report(con, labels_dir: Path, run: str, config: dict) -> dict:
    src = f"read_parquet({_q(labels_dir / run / 'month=*' / 'labels.parquet')})"
    dev = f"(SELECT * FROM {src} WHERE split_period = 'development')"
    out = {"rows": _one(con, f"SELECT COUNT(*) AS n FROM {dev}")["n"]}
    durations = config["episode"].get("min_duration_hours_variants", [])
    for h in config["horizons_hours"]:
        y = f"y_{h}h"
        block = _one(
            con,
            f"""SELECT COUNT({y}) AS evaluable, SUM({y}) AS positives,
                   ROUND(AVG({y}), 6) AS positive_rate,
                   ROUND(AVG({y}) FILTER (WHERE candidate_past_7d), 5) AS p_after_candidate_7d,
                   ROUND(AVG({y}) FILTER (WHERE NOT candidate_past_7d), 6) AS p_clean_7d,
                   COUNT({y}) FILTER (WHERE candidate_past_7d) AS evaluable_after_candidate_7d,
                   COUNT(*) FILTER (WHERE outcome_{h}h = 'excluded') AS excluded
                FROM {dev}""",
        )
        for d in durations:
            tag = sft._tag(float(d))
            block[f"positive_rate_dur{tag}h"] = _one(
                con, f"SELECT ROUND(AVG(y_{h}h_dur{tag}h), 6) AS r FROM {dev}"
            )["r"]
        block["exclusions"] = {
            r["reason"]: r["n"]
            for r in _records(
                con,
                f"""SELECT exclusion_reason_{h}h AS reason, COUNT(*) AS n FROM {dev}
                    WHERE exclusion_reason_{h}h IS NOT NULL GROUP BY 1 ORDER BY 1""",
            )
        }
        daily = f"""(SELECT issued_at, SUM({y}) AS p,
                        COUNT(DISTINCT cluster_id_{h}h) FILTER (WHERE {y} = 1) AS ev
                     FROM {dev} WHERE {y} IS NOT NULL GROUP BY issued_at)"""
        if h < 168:
            block["daily"] = _one(
                con,
                f"""SELECT COUNT(*) AS days, ROUND(AVG(p), 2) AS positives_per_day,
                       ROUND(AVG(ev), 2) AS events_per_day,
                       ROUND(SUM(LEAST(p, 10)) / SUM(p), 4) AS ceiling_channels_10,
                       ROUND(SUM(LEAST(p, 20)) / SUM(p), 4) AS ceiling_channels_20,
                       ROUND(SUM(LEAST(ev, 10)) / SUM(ev), 4) AS ceiling_events_10,
                       ROUND(SUM(LEAST(ev, 20)) / SUM(ev), 4) AS ceiling_events_20
                    FROM {daily}""",
            )
        else:
            block["weekly_monday"] = _one(
                con,
                f"""SELECT COUNT(*) AS weeks, ROUND(AVG(p), 2) AS positives_per_week,
                       ROUND(AVG(ev), 2) AS events_per_week,
                       ROUND(SUM(LEAST(p, 20)) / SUM(p), 4) AS ceiling_channels_20,
                       ROUND(SUM(LEAST(p, 50)) / SUM(p), 4) AS ceiling_channels_50,
                       ROUND(SUM(LEAST(ev, 20)) / SUM(ev), 4) AS ceiling_events_20,
                       ROUND(SUM(LEAST(ev, 50)) / SUM(ev), 4) AS ceiling_events_50
                    FROM {daily} WHERE dayofweek(issued_at) = 1""",
            )
        block["by_sensor_type"] = _records(
            con,
            f"""SELECT sensor_type, COUNT({y}) AS evaluable, SUM({y}) AS positives,
                   ROUND(AVG({y}), 6) AS positive_rate,
                   ROUND(AVG({y}) FILTER (WHERE candidate_past_7d), 4) AS p_after_candidate_7d,
                   ROUND(AVG({y}) FILTER (WHERE NOT candidate_past_7d), 6) AS p_clean_7d
                FROM {dev} GROUP BY 1 HAVING SUM({y}) > 0 ORDER BY positives DESC""",
        )
        out[f"{h}h"] = block
    return out


ALL_MONTHS = study_months("2019-01", "2026-06")


def _counts(con, path_glob: str, group: str, y: str, outcome: str) -> list[dict]:
    return _records(
        con,
        f"""SELECT {group}, COUNT(*) AS n_rows, COUNT({y}) AS evaluable,
               COALESCE(SUM({y}), 0) AS positives
            FROM read_parquet({_q(path_glob)}) GROUP BY ALL ORDER BY ALL""",
    )


def build_model_labels(
    con,
    tables: Path,
    output: Path,
    config: dict,
    *,
    model_config: str | None = None,
    labels_dir: str = "model_labels",
    months: list[str] | None = None,
    targets: list[str] | None = None,
    cutoff_hour: int | None = None,
) -> dict:
    """Frozen-protocol labels, cards, links and events for every target and month.

    v1 (default): label-config targets into ``model_labels/``; v2: pass
    ``configs/sensor_failure_model_v2.json`` and ``model_labels_v2``. ``months``,
    ``targets`` and ``cutoff_hour`` (v3 prep: development months at 06:00)
    narrow the build; defaults reproduce v1/v2.
    """
    model_root = output / labels_dir
    model = sft.load_model_config(config, model_config)
    declared = [
        t
        for t, d in model.get("targets", {}).items()
        if "label" in d and not isinstance(d.get("episode_filter"), str)
    ] or list(config["targets"])
    if targets:
        unknown = sorted(set(targets) - set(declared))
        if unknown:
            raise ValueError(f"targets not in the model config: {unknown}")
    targets = [t for t in declared if not targets or t in targets]
    months = months or ALL_MONTHS
    summary = {
        "model_version": model["version"],
        "months": [months[0], months[-1]],
        "targets": {},
        "timings_s": {},
    }
    if months != ALL_MONTHS:
        summary["month_list"] = months
    if cutoff_hour:
        summary["cutoff_hour"] = int(cutoff_hour)
    spec = sft.target_spec(targets[0], config, model_config=model_config)
    sft.register_failure_tables(con, tables, q_hours=spec["q_hours"], w_minutes=spec["w_minutes"])
    for target in targets:
        t0 = time.time()
        spec = sft.target_spec(target, config, model_config=model_config)
        sft.materialize_target_events(
            con, target, model_root / "events", config=config, model_config=model_config
        )
        for month in months:
            sft.label_target_month(
                con,
                month,
                model_root,
                target=target,
                config=config,
                model_config=model_config,
                cutoff_hour=cutoff_hour,
            )
        summary["timings_s"][target] = round(time.time() - t0, 1)
        # A partial build (e.g. development only) reports events of its own splits only.
        events_scope = (
            ""
            if months == ALL_MONTHS
            else "WHERE split_period IN (SELECT DISTINCT split_period FROM read_parquet("
            f"{_q(model_root / f'channels/{target}/*.parquet')}))"
        )
        block = {
            "channels": {
                f"{h}h": _counts(
                    con,
                    str(model_root / f"channels/{target}/*.parquet"),
                    "split_period",
                    f"y_{h}h",
                    f"outcome_{h}h",
                )
                for h in spec["horizons"]
            },
            "cards": {
                f"{h}h": _counts(
                    con,
                    str(model_root / f"cards/{target}/{h}h/*.parquet"),
                    "split_period",
                    "y_card",
                    "outcome",
                )
                for h in spec["horizons"]
            },
            "events": _records(
                con,
                f"""SELECT split_period, COUNT(*) AS events, COUNT_IF(qualifies) AS qualifying,
                       COUNT_IF(qualifies AND mass) AS qualifying_mass
                    FROM read_parquet({_q(model_root / f"events/{target}/events.parquet")})
                    {events_scope} GROUP BY 1 ORDER BY 1""",
            ),
        }
        summary["targets"][target] = block
        print(f"model labels {target}: {summary['timings_s'][target]} s", flush=True)
    (model_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "data-science/artifacts/sensor-failure-v1"
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "data-science/reports/sensor-failure-measurement-2026-09-26.json",
    )
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--memory", default="10GB")
    parser.add_argument("--reuse-tables", action="store_true")
    parser.add_argument(
        "--model-labels",
        action="store_true",
        help="build frozen-protocol labels/cards/events for all months instead of measuring",
    )
    parser.add_argument("--model-config", default=None, help="default: v1 model config")
    parser.add_argument("--labels-dir", default="model_labels")
    parser.add_argument(
        "--tables",
        type=Path,
        default=None,
        help="read-only failure tables (never rebuilt); default: OUTPUT/tables",
    )
    parser.add_argument(
        "--cutoff-hour", type=int, default=None, help="model labels: daily issue hour (default 0)"
    )
    parser.add_argument(
        "--label-months",
        choices=["all", "development"],
        default="all",
        help="model labels: all months 2019-01..2026-06 or development months only",
    )
    parser.add_argument("--targets", nargs="*", default=None, help="model labels: subset")
    args = parser.parse_args()
    config = sft.load_config()
    started = time.time()
    con, meta = open_fire_source(ROOT)
    con.execute(f"SET threads={int(args.threads)}")
    con.execute(f"SET memory_limit='{args.memory}'")
    register_registry_views(con, ROOT / config["source"]["registry"])
    tables = args.tables or args.output / "tables"
    timings = {}
    if args.tables is not None:
        if not (tables / "manifest.json").exists():
            raise FileNotFoundError(tables / "manifest.json")
    elif not (args.reuse_tables and (tables / "manifest.json").exists()):
        t0 = time.time()
        sft.materialize_failure_tables(con, tables, config=config)
        timings["materialize_s"] = round(time.time() - t0, 1)
    table_manifest = json.loads((tables / "manifest.json").read_text(encoding="utf-8"))
    if args.model_labels:
        t0 = time.time()
        build_model_labels(
            con,
            tables,
            args.output,
            config,
            model_config=args.model_config,
            labels_dir=args.labels_dir,
            months=DEV_MONTHS if args.label_months == "development" else None,
            targets=args.targets,
            cutoff_hour=args.cutoff_hour,
        )
        print(f"model labels done in {time.time() - t0:.0f} s ({timings})")
        return
    w = int(config["cluster"]["w_minutes"])
    runs = [(label, q) for label in MAIN_LABELS for q in config["episode"]["q_hours_grid"]]
    runs += [(label, 24) for label in config["labels"] if label not in MAIN_LABELS]
    t0 = time.time()
    for label, q in runs:
        sft.register_failure_tables(con, tables, q_hours=q, w_minutes=w)
        for month in DEV_MONTHS:
            sft.label_month(
                con,
                month,
                args.output / "labels",
                label=label,
                q_hours=q,
                w_minutes=w,
                config=config,
            )
        print(f"labels {label} Q={q}h done ({time.time() - t0:.0f} s)", flush=True)
    timings["labels_s"] = round(time.time() - t0, 1)
    report = {
        "study": "sensor-failure-v1 label measurement",
        "status": "development only; descriptive; no model fit; not a validation result",
        "created_by": "data-science/scripts/measure_sensor_failure.py",
        "git_sha": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip(),
        "git_dirty": bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
        ),
        "source": meta,
        "config": {
            "version": config["version"],
            "labels": config["labels"],
            "horizons_hours": config["horizons_hours"],
            "episode": config["episode"],
            "cluster": config["cluster"],
        },
        "technical_value_provider": table_manifest["technical_value_provider"],
        "tables": {k: v for k, v in table_manifest.items() if k in ("episodes", "files")},
        "development": "starts/cutoffs in 2019-01-31..2020-12-31 and 2022-01-31..2024-12-31",
    }
    report["episodes"] = episodes_report(con, tables, config)
    report["claim_record_level_10min"] = record_level_claim(con, tables)
    report["alarm_co_occurrence"] = co_occurrence(con, tables)
    report["labels"] = {
        f"{label}_q{q}h": labels_report(con, args.output / "labels", f"{label}_q{q}h_w{w}m", config)
        for label, q in runs
    }
    timings["total_s"] = round(time.time() - started, 1)
    report["timings"] = timings
    args.report.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, ensure_ascii=False, indent=1, default=str) + "\n"
    args.report.write_text(text, encoding="utf-8")
    (args.output / "measurement.json").write_text(text, encoding="utf-8")
    print(f"report: {args.report} ({timings})")


if __name__ == "__main__":
    main()

"""Build the v4 feature groups N/E/K/S/R/T/W for development months (00:00 cutoffs).

Inputs (read only): the subsystem-alarm-v1 channel registry, the object reference of
the curated snapshot (level-2 parent = area), episodes/events of the label from
``model_labels_v2/events/connection_loss_gt2s``, candidate timestamps of the
label from ``tables/candidates.parquet`` and, for W, the journal (``working_events``
of ``open_fire_source``, four record texts only). Rows after 2024 are dropped on read:
calibration and holdout are never loaded. Output per group:
``artifacts/sensor-failure-v1/features_v4/<group>/month=YYYY-MM/features.parquet``
keyed by (channel_id, issued_at), plus the monthly node snapshot in
``features_v4/nodes/month=YYYY-MM/nodes.parquet`` and a manifest (all gitignored).

    python data-science/scripts/build_sensor_failure_v4_features.py [--months 2024-03 ...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from time import perf_counter

import duckdb
import pandas as pd

from infra_pulse_research.modeling import sensor_failure_fe_v4 as fe
from infra_pulse_research.modeling import sensor_failure_nodes as nodes_mod
from infra_pulse_research.modeling.fire_source import open_fire_source
from infra_pulse_research.modeling.subsystem_grid import study_months

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "data-science/configs/sensor_failure_tuning_v4.json"
ART = ROOT / "data-science/artifacts"
SF = ART / "sensor-failure-v1"
DEV_END = "2025-01-01"
DEV_MONTHS = [m for m in study_months("2019-01", "2024-12") if not m.startswith("2021")]


def _q(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _sha(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_inputs(
    con: duckdb.DuckDBPyConnection,
    *,
    registry: Path,
    events_dir: Path,
    tables: Path,
    objects: Path | None,
    label: str = "connection_loss",
    end: str = DEV_END,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    reg = con.execute(f"""
        SELECT channel_id, object_id, system_type, sensor_type, first_seen
        FROM read_parquet({_q(registry)})
    """).df()
    if objects is not None and Path(objects).exists():
        area = con.execute(f"""
            SELECT TRY_CAST("ид_объект" AS BIGINT) AS object_id,
                   TRY_CAST("родитель" AS BIGINT) AS area_id
            FROM read_parquet({_q(objects)})
            WHERE TRY_CAST("иерархия_уровень" AS INTEGER) = 3
        """).df()
        reg = reg.merge(area, on="object_id", how="left")
    else:
        reg["area_id"] = pd.NA
    members = con.execute(f"""
        SELECT event_id, channel_id, object_id, sensor_type, start_at, end_at, qualifying
        FROM read_parquet({_q(Path(events_dir) / "event_members.parquet")})
        WHERE start_at < TIMESTAMP {_q(end)}
    """).df()
    # Ends at or after the development end are unknown here; treat them as open.
    members.loc[members["end_at"] >= pd.Timestamp(end), "end_at"] = pd.NaT
    candidates = con.execute(f"""
        SELECT channel_id, event_at FROM read_parquet({_q(Path(tables) / "candidates.parquet")})
        WHERE label = {_q(label)} AND event_at < TIMESTAMP {_q(end)}
    """).df()
    return reg, members, candidates


def build(
    months: list[str],
    output: Path,
    *,
    registry: Path,
    events_dir: Path,
    tables: Path,
    objects: Path | None,
    config: dict,
    groups=fe.GROUPS,
) -> list[dict]:
    con = duckdb.connect()
    con.execute("SET threads=6; SET memory_limit='10GB'")
    reg, members, candidates = load_inputs(
        con, registry=registry, events_dir=events_dir, tables=tables, objects=objects
    )
    signals = None
    if "W" in groups:
        journal, _ = open_fire_source(ROOT)
        journal.execute("SET threads=6; SET memory_limit='10GB'")
        signals = fe.work_signals(journal, "working_events", DEV_END, month_column="month")
        journal.close()
    cal = config["groups"]["K"]["holidays"]
    holidays = fe.holidays(range(cal["years"][0], cal["years"][1] + 1), cal["month_days"])
    log = []
    for month in months:
        t0 = perf_counter()
        builder = fe.V4Month(
            con,
            reg,
            members,
            candidates,
            month,
            episode_filter=config["target"]["episode_filter"],
            holiday_dates=holidays,
            signals=signals,
        )
        frames = builder.build(groups)
        entry = {"month": month, "rows": len(builder.grid), "groups": {}}
        snap = output / "nodes" / f"month={month}"
        snap.mkdir(parents=True, exist_ok=True)
        builder.nodes.to_parquet(snap / "nodes.parquet", index=False)
        entry["nodes"] = nodes_mod.snapshot_summary(builder.nodes)
        for group, frame in frames.items():
            part = output / group / f"month={month}"
            part.mkdir(parents=True, exist_ok=True)
            path = part / "features.parquet"
            frame.to_parquet(path, index=False)
            entry["groups"][group] = {
                "columns": len(frame.columns) - 2,
                "sha256": _sha(path),
            }
        entry["seconds"] = round(perf_counter() - t0, 1)
        log.append(entry)
        print(month, entry["rows"], "rows,", entry["seconds"], "s", flush=True)
    return log


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--months", nargs="*", default=None, help="default: development months")
    parser.add_argument("--output", type=Path, default=SF / "features_v4")
    parser.add_argument("--registry", type=Path, default=ART / "subsystem-alarm-v1/registry")
    parser.add_argument(
        "--events-dir", type=Path, default=SF / "model_labels_v2/events/connection_loss_gt2s"
    )
    parser.add_argument("--tables", type=Path, default=SF / "tables")
    parser.add_argument(
        "--objects",
        type=Path,
        default=ART / "curated-all-sensors-exclude-2021-q2/objects.parquet",
    )
    parser.add_argument("--groups", nargs="*", default=list(fe.GROUPS))
    args = parser.parse_args()
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    registry = args.registry
    if registry.is_dir():
        registry = registry / "channel_registry.parquet"
    started = perf_counter()
    log = build(
        args.months or DEV_MONTHS,
        args.output,
        registry=registry,
        events_dir=args.events_dir,
        tables=args.tables,
        objects=args.objects,
        config=config,
        groups=args.groups,
    )
    manifest = {
        "feature_version": "sensor-failure-features-v4",
        "config": str(CONFIG.relative_to(ROOT)),
        "config_sha256": _sha(CONFIG),
        "inputs": {
            "registry": _sha(registry),
            "event_members": _sha(args.events_dir / "event_members.parquet"),
            "candidates": _sha(args.tables / "candidates.parquet"),
        },
        "months": log,
        "seconds": round(perf_counter() - started, 1),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"total {manifest['seconds']} s, {len(log)} months")


if __name__ == "__main__":
    main()

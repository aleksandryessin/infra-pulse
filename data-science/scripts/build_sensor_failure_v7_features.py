"""Build the v7 pair features (object × sensor type × 00:00 cutoff) for development months.

Inputs (read only): the card universe of ``model_labels_v5/cards/connection_loss_ge2s/168h``
(the same keys as 336h), the target's event members
(``model_labels_v5/events/connection_loss_ge2s``), the ``subsystem-alarm-v1`` channel
registry with the level-2 parent of the object as area, and journal day aggregates of
``working_events`` (``open_fire_source``): "Неопределен" per pair and day, messages per
object and day, pump/fan "Обесточен" records. Rows at or after 2025-01-01 are dropped on
read: calibration and holdout are never loaded. Output (gitignored):
``artifacts/sensor-failure-v1/features_v7/month=YYYY-MM/features.parquet`` keyed by
(object_id, sensor_type, issued_at) and ``manifest.json``.

Sanity check at build time: ``p7h__events_365d`` equals the static list of
``sensor_failure_evaluation.static_list_scores`` on every card.

    python data-science/scripts/build_sensor_failure_v7_features.py [--months 2024-03 ...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from time import perf_counter

import duckdb
import numpy as np
import pandas as pd

from infra_pulse_research.modeling import sensor_failure_evaluation as ev
from infra_pulse_research.modeling import sensor_failure_fe_v7 as fe7
from infra_pulse_research.modeling.fire_source import open_fire_source

ROOT = Path(__file__).resolve().parents[2]
ART = ROOT / "data-science/artifacts"
SF = ART / "sensor-failure-v1"
LABELS = SF / "model_labels_v5"
TARGET = "connection_loss_ge2s"
RULE = {"op": ">=", "seconds": 2}
DEV_END = "2025-01-01"
JOURNAL_START = "2018-11-01"
DEV_MONTHS = [f"{y}-{m:02d}" for y in (2019, 2020, 2022, 2023, 2024) for m in range(1, 13)]
HOLIDAY_YEARS = range(2018, 2026)


def _q(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _files(paths) -> str:
    return "[" + ", ".join(_q(p) for p in paths) + "]"


def _sha(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_registry(con, registry: Path, objects: Path | None) -> pd.DataFrame:
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
    return reg


def load_members(con, events_dir: Path, end: str = DEV_END) -> pd.DataFrame:
    members = con.execute(f"""
        SELECT event_id, channel_id, object_id, sensor_type, start_at, end_at,
               duration_hours, qualifying
        FROM read_parquet({_q(Path(events_dir) / "event_members.parquet")})
        WHERE start_at < TIMESTAMP {_q(end)}
    """).df()
    # Ends at or after the data end are not known here: the episode is open.
    late = members["end_at"] >= pd.Timestamp(end)
    members.loc[late, "end_at"] = pd.NaT
    members.loc[late | members["end_at"].isna(), "duration_hours"] = np.nan
    return members


def load_cards(con, months, end: str = DEV_END) -> pd.DataFrame:
    files = _files(LABELS / f"cards/{TARGET}/168h/{m}.parquet" for m in months)
    return con.execute(f"""
        SELECT object_id, sensor_type, system_type, issued_at, month,
               known_channels, candidate_channels
        FROM read_parquet({files}) WHERE issued_at < TIMESTAMP {_q(end)}
        ORDER BY issued_at, object_id, sensor_type
    """).df()


def build(
    months: list[str],
    output: Path,
    *,
    registry: Path,
    objects: Path | None,
    events_dir: Path,
) -> dict:
    started = perf_counter()
    con = duckdb.connect()
    con.execute("SET threads=3; SET memory_limit='6GB'")
    reg = load_registry(con, registry, objects)
    members = load_members(con, events_dir)
    cards = load_cards(con, months)
    journal, _ = open_fire_source(ROOT)
    journal.execute("SET threads=3; SET memory_limit='6GB'")
    pair_days, object_days, signals = fe7.journal_day_aggregates(
        journal, "working_events", DEV_END, start=JOURNAL_START, month_column="month"
    )
    journal.close()
    t_inputs = round(perf_counter() - started, 1)
    print(f"inputs: {len(cards)} cards, {len(members)} members ({t_inputs} s)", flush=True)
    builder = fe7.PairFeatures(
        cards,
        members,
        reg,
        rule=RULE,
        pair_days=pair_days,
        object_days=object_days,
        signals=signals[["object_id", "ts"]],
        holiday_dates=fe7.holidays(HOLIDAY_YEARS),
    )
    timings = {}
    frames = {}
    for group in fe7.GROUPS:
        t0 = perf_counter()
        frames[group] = getattr(builder, f"group_{group.lower()}")()
        timings[group] = round(perf_counter() - t0, 1)
        print(f"group {group}: {frames[group].shape[1]} columns ({timings[group]} s)", flush=True)
    out = cards[fe7.KEYS].copy()
    out["cat__sensor_type"] = cards["sensor_type"].astype(str).to_numpy()
    out["cat__system_type"] = cards["system_type"].astype(str).to_numpy()
    for frame in frames.values():
        for col in frame.columns:
            out[col] = frame[col].to_numpy()
    static = ev.static_list_scores(cards, members, RULE).to_numpy()
    mismatch = int((out["p7h__events_365d"].to_numpy() != static).sum())
    if mismatch:
        raise ValueError(f"p7h__events_365d differs from the static list on {mismatch} cards")
    if out.duplicated(fe7.KEYS).any():
        raise ValueError("duplicate card keys")
    log = []
    for month, part in out.groupby(cards["month"].to_numpy(), sort=True):
        path = output / f"month={month}" / "features.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        part.reset_index(drop=True).to_parquet(path, index=False)
        log.append({"month": month, "rows": len(part), "sha256": _sha(path)})
    coverage = {
        c: {
            "non_missing": round(float(out[c].notna().mean()), 4),
            "non_zero": round(float((out[c].fillna(0) != 0).mean()), 4),
        }
        for c in out.columns
        if c.startswith(tuple(fe7.PREFIX.values()))
    }
    return {
        "feature_version": "sensor-failure-features-v7",
        "rows": len(out),
        "columns": [c for c in out.columns if c not in fe7.KEYS],
        "static_list_check": "p7h__events_365d == static_list_scores on every card",
        "inputs": {
            "registry": _sha(registry),
            "event_members": _sha(Path(events_dir) / "event_members.parquet"),
            "pair_day_rows": len(pair_days),
            "object_day_rows": len(object_days),
            "pumpfan_deenergized_records": len(signals),
        },
        "coverage": coverage,
        "timings_seconds": {"inputs": t_inputs, **timings},
        "months": log,
        "seconds": round(perf_counter() - started, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--months", nargs="*", default=None, help="default: development months")
    parser.add_argument("--output", type=Path, default=SF / "features_v7")
    parser.add_argument(
        "--registry",
        type=Path,
        default=ART / "subsystem-alarm-v1/registry/channel_registry.parquet",
    )
    parser.add_argument(
        "--objects", type=Path, default=ART / "curated-all-sensors-exclude-2021-q2/objects.parquet"
    )
    parser.add_argument("--events-dir", type=Path, default=LABELS / f"events/{TARGET}")
    args = parser.parse_args()
    manifest = build(
        args.months or DEV_MONTHS,
        args.output,
        registry=args.registry,
        objects=args.objects,
        events_dir=args.events_dir,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"total {manifest['seconds']} s, {manifest['rows']} rows")


if __name__ == "__main__":
    main()

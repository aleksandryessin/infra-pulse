"""Compare archive CSV, legacy Parquet and accepted curated boundary aggregates.

This is a structural/accounting check, not proof that the old converter preserved
every value. Only the requested non-2021 archive years are read.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import resource
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import duckdb
import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "data-science/scripts"))
from curate_journal import HEADER, archive  # noqa: E402

YEARS = (2019, 2020, 2022, 2023, 2024, 2025, 2026)


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def raw_year(source: Path, year: int) -> dict:
    count = 0
    technical_headers = 0
    min_ts: str | None = None
    max_ts: str | None = None
    alarms = Counter()
    sample = Counter()
    with archive(source / f"ext-journal-{year}.7z") as stream:
        rows = csv.reader(stream)
        header = next(rows)
        if header != HEADER:
            raise ValueError(f"archive {year} has unexpected header")
        for row in rows:
            if row == HEADER:
                technical_headers += 1
                continue
            if len(row) != 6:
                raise ValueError(f"archive {year} has record with {len(row)} columns")
            count += 1
            ts = row[2] + " " + row[3]
            if min_ts is None or ts < min_ts:
                min_ts = ts
            if max_ts is None or ts > max_ts:
                max_ts = ts
            alarm = row[4].lower()
            if alarm in {"t", "true"}:
                alarms["true"] += 1
            elif alarm in {"f", "false"}:
                alarms["false"] += 1
            else:
                alarms["invalid"] += 1
            if int(row[0]) % 10000 == 0:
                sample[
                    (
                        int(row[0]),
                        int(row[1]),
                        row[2],
                        dt.time.fromisoformat(row[3]).isoformat(),
                        alarm in {"t", "true"},
                        row[5],
                    )
                ] += 1
    return {
        "columns": HEADER,
        "records": count,
        "technical_headers": technical_headers,
        "min_local_timestamp": min_ts,
        "max_local_timestamp": max_ts,
        "alarm_boolean_counts": dict(alarms),
        "_sample": sample,
    }


def legacy_year(year: int) -> dict:
    path = ROOT / f"data-science/data/parquet/year={year}/data_0.parquet"
    parquet = pq.ParquetFile(path)
    i = parquet.schema_arrow.names.index("event_date")
    dates = [
        parquet.metadata.row_group(group).column(i).statistics
        for group in range(parquet.metadata.num_row_groups)
    ]
    if not all(item and item.has_min_max for item in dates):
        raise ValueError(f"legacy {year} lacks event_date statistics")
    alarm_true = 0
    alarm_invalid = 0
    sample = Counter()
    for group in range(parquet.metadata.num_row_groups):
        table = parquet.read_row_group(
            group,
            columns=["event_id", "channel_id", "event_date", "event_time", "is_alarm", "raw_value"],
        )
        flags = table["is_alarm"]
        alarm_true += pc.sum(flags).as_py() or 0
        alarm_invalid += flags.null_count
        matches = np.flatnonzero(table["event_id"].to_numpy() % 10000 == 0)
        for index in matches:
            cells = [
                table[name][int(index)].as_py()
                for name in (
                    "event_id",
                    "channel_id",
                    "event_date",
                    "event_time",
                    "is_alarm",
                    "raw_value",
                )
            ]
            sample[
                (cells[0], cells[1], str(cells[2]), cells[3].isoformat(), cells[4], cells[5])
            ] += 1
    return {
        "columns_and_types": [str(field) for field in parquet.schema_arrow],
        "records": parquet.metadata.num_rows,
        "min_date": str(min(item.min for item in dates)),
        "max_date": str(max(item.max for item in dates)),
        "alarm_true_count": alarm_true,
        "alarm_invalid_count": alarm_invalid,
        "_sample": sample,
        "file_sha256": sha(path),
    }


def curated_by_source(snapshot: Path) -> dict:
    connection = duckdb.connect()
    connection.execute("SET threads=2")
    connection.execute("SET memory_limit='4GB'")
    out: dict[str, dict] = {}
    try:
        for month in sorted((snapshot / "curated").glob("month=*")):
            rows = connection.execute(
                "SELECT source_file,sensor_type,count(*),min(event_ts_local_raw),"
                "max(event_ts_local_raw),count(*) FILTER (WHERE is_epoch_placeholder) "
                "FROM read_parquet(?) GROUP BY source_file,sensor_type",
                [str(month / "events.parquet")],
            ).fetchall()
            for name, sensor_type, count, low, high, epoch_count in rows:
                entry = out.setdefault(
                    name,
                    {
                        "records": 0,
                        "min_local_timestamp": low,
                        "max_local_timestamp": high,
                        "epoch_placeholder_count": 0,
                        "epoch_by_sensor_type": {},
                    },
                )
                entry["records"] += count
                entry["min_local_timestamp"] = min(entry["min_local_timestamp"], low)
                entry["max_local_timestamp"] = max(entry["max_local_timestamp"], high)
                entry["epoch_placeholder_count"] += epoch_count
                if epoch_count:
                    kind = sensor_type or "<unknown>"
                    by_type = entry["epoch_by_sensor_type"]
                    by_type[kind] = by_type.get(kind, 0) + epoch_count
    finally:
        connection.close()
    return out


def run(source: Path, snapshot: Path, output: Path) -> dict:
    started = time.monotonic()
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    if manifest["publication_status"] != "accepted":
        raise ValueError("accepted DATA-03 snapshot required")
    if set(manifest["years"]) != set(YEARS):
        raise ValueError("snapshot years differ from non-2021 verification scope")
    years = {}
    for year in YEARS:
        raw = raw_year(source, year)
        legacy = legacy_year(year)
        raw_sample = raw.pop("_sample")
        legacy_sample = legacy.pop("_sample")
        years[str(year)] = {
            "archive": raw,
            "legacy_parquet": legacy,
            "raw_vs_legacy_record_count_equal": raw["records"] == legacy["records"],
            "raw_vs_legacy_date_bounds_equal": (
                raw["min_local_timestamp"][:10] == legacy["min_date"]
                and raw["max_local_timestamp"][:10] == legacy["max_date"]
            ),
            "raw_vs_legacy_alarm_counts_equal": (
                raw["alarm_boolean_counts"].get("true", 0) == legacy["alarm_true_count"]
                and raw["alarm_boolean_counts"].get("invalid", 0) == legacy["alarm_invalid_count"]
            ),
            "deterministic_sample_modulus": 10000,
            "deterministic_sample_records": sum(raw_sample.values()),
            "deterministic_sample_multiset_equal": raw_sample == legacy_sample,
            "deterministic_sample_missing_from_legacy": sum((raw_sample - legacy_sample).values()),
            "deterministic_sample_extra_in_legacy": sum((legacy_sample - raw_sample).values()),
        }
    curated = curated_by_source(snapshot)
    for year in YEARS:
        entry = years[str(year)]
        new = curated[f"ext-journal-{year}.7z"]
        entry["curated"] = new
        entry["raw_vs_curated_record_accounting_equal"] = (
            entry["archive"]["records"]
            == new["records"] + manifest["sources"][str(year)]["N_dedup_removed"]
        )
        entry["raw_vs_curated_date_bounds_equal"] = (
            entry["archive"]["min_local_timestamp"] == new["min_local_timestamp"]
            and entry["archive"]["max_local_timestamp"] == new["max_local_timestamp"]
        )
    report = {
        "version": "source-parquet-verification-v1",
        "scope": "2019, 2020, 2022–2026; 2021 excluded by study constraint",
        "models_fitted": 0,
        "years": years,
        "dataset_manifest_sha256": sha(snapshot / "manifest.json"),
        "config_sha256": hashlib.sha256(str(YEARS).encode()).hexdigest(),
        "code_sha256": sha(Path(__file__)),
        "git_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "git_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
        ),
        "runtime": {
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**2, 1),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.source, args.snapshot, args.output)
    print(
        json.dumps(
            {
                year: {key: value for key, value in details.items() if key.endswith("equal")}
                for year, details in result["years"].items()
            }
        )
    )

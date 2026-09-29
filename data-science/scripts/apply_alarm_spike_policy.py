"""Publish a reversible, sensor-aware research exclusion overlay on curated v4.

The accepted source snapshot stays immutable. Excluded curated rows are copied
with provenance into an ignored local artifact; the companion DuckDB view in
``infra_pulse_research.data.prepared`` applies the same policy lazily.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import resource
import shutil
import time
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

VERSION = "alarm-spike-exclusions-v1"
RULES = [
    {
        "start": "2020-02-01",
        "end": "2020-04-01",
        "sensor_type": None,
        "reason": "reviewed_all_sensor_feb_mar_2020_spike",
    },
    {
        "start": "2021-07-02",
        "end": "2021-07-03",
        "sensor_type": "Газовый датчик",
        "reason": "reviewed_gas_2021_boundary_spike",
    },
    {
        "start": "2025-11-06",
        "end": "2025-11-07",
        "sensor_type": "Газовый датчик",
        "reason": "reviewed_gas_nov_2025_spike",
    },
]


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def validate_rules(rules: list[dict]) -> None:
    for rule in rules:
        if dt.date.fromisoformat(rule["start"]) >= dt.date.fromisoformat(rule["end"]):
            raise ValueError("exclusion interval must be nonempty")
    for index, left in enumerate(rules):
        for right in rules[index + 1 :]:
            overlapping_time = left["start"] < right["end"] and right["start"] < left["end"]
            overlapping_scope = (
                left["sensor_type"] is None
                or right["sensor_type"] is None
                or left["sensor_type"] == right["sensor_type"]
            )
            if overlapping_time and overlapping_scope:
                raise ValueError("exclusion rules overlap for the same sensor scope")


def rule_sql(rule: dict, *, alias: str = "e") -> str:
    day = f"TRY_CAST(substr({alias}.event_ts_local_raw, 1, 10) AS DATE)"
    sensor = (
        ""
        if rule["sensor_type"] is None
        else f" AND {alias}.sensor_type = {quote(rule['sensor_type'])}"
    )
    return f"({day} >= DATE {quote(rule['start'])} AND {day} < DATE {quote(rule['end'])}{sensor})"


def months_touched(rule: dict) -> list[str]:
    current = dt.date.fromisoformat(rule["start"]).replace(day=1)
    last = (dt.date.fromisoformat(rule["end"]) - dt.timedelta(days=1)).replace(day=1)
    months = []
    while current <= last:
        months.append(current.strftime("%Y-%m"))
        current = (
            dt.date(current.year + 1, 1, 1)
            if current.month == 12
            else dt.date(current.year, current.month + 1, 1)
        )
    return months


def run(snapshot: Path, output: Path) -> dict:
    validate_rules(RULES)
    start = time.monotonic()
    snapshot = snapshot.resolve()
    output = output.resolve()
    if output == snapshot or output.is_relative_to(snapshot):
        raise ValueError("overlay must be outside the immutable source snapshot")
    partial = output.with_name(output.name + ".partial")
    if output.exists() or partial.exists():
        raise FileExistsError("overlay or partial already exists; no overwrite")
    base_manifest_path = snapshot / "manifest.json"
    base = json.loads(base_manifest_path.read_text(encoding="utf-8"))
    if base["publication_status"] != "accepted":
        raise ValueError("base snapshot is not accepted")
    if base["exclusion_policy"]["version"] != "exclude-2021-apr-jun-v1":
        raise ValueError("overlay requires the eight-year Apr–Jun 2021 base policy")
    if base["years"] != list(range(2019, 2027)):
        raise ValueError("overlay requires all eight archive years")
    partial.mkdir(parents=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='4GB'")
    con.execute(f"SET temp_directory={quote(partial / 'spill')}")
    try:
        by_rule = []
        for rule in RULES:
            for month in months_touched(rule):
                path = snapshot / "curated" / f"month={month}" / "events.parquet"
                if not path.is_file():
                    raise FileNotFoundError(path)
                predicate = rule_sql(rule)
                source = f"read_parquet({quote(path)}) e"
                n, alarms = con.execute(
                    f"SELECT count(*), count(*) FILTER (WHERE e.alarm) "
                    f"FROM {source} WHERE {predicate}"
                ).fetchone()
                if n == 0:
                    raise ValueError(f"empty policy selection: {rule['reason']}, {month}")
                folder = partial / "excluded" / f"month={month}"
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / f"{rule['reason']}.parquet"
                con.execute(
                    f"COPY (SELECT e.*, {quote(rule['reason'])} policy_reason, "
                    f"{quote(VERSION)} policy_version FROM {source} "
                    f"WHERE {predicate} ORDER BY e.source_file, e.record_ordinal) "
                    f"TO {quote(target)} (FORMAT PARQUET, COMPRESSION ZSTD)"
                )
                copied = pq.ParquetFile(target).metadata.num_rows
                if copied != n:
                    raise AssertionError(f"excluded copy mismatch in {month}: {copied} != {n}")
                by_rule.append({**rule, "month": month, "excluded_rows": n, "alarm_true": alarms})
        # The day table is a policy calendar. It is not a claim that the source
        # stopped observing: other sensor types remain available on gas-only days.
        days = []
        for rule in RULES:
            current = dt.date.fromisoformat(rule["start"])
            end = dt.date.fromisoformat(rule["end"])
            while current < end:
                days.append(
                    {
                        "day": current,
                        "sensor_type_scope": rule["sensor_type"],
                        "reason": rule["reason"],
                        "policy_version": VERSION,
                    }
                )
                current += dt.timedelta(days=1)
        pq.write_table(
            pa.Table.from_pylist(days), partial / "policy_days.parquet", compression="zstd"
        )
        output_hashes = {
            path.relative_to(partial).as_posix(): sha256(path)
            for path in sorted(partial.rglob("*.parquet"))
        }
        total = sum(item["excluded_rows"] for item in by_rule)
        report = {
            "policy_version": VERSION,
            "status": "accepted_research_overlay_not_training_approval",
            "base_manifest_sha256": sha256(base_manifest_path),
            "base_etl_version": base["etl_version"],
            "base_exclusion_policy": base["exclusion_policy"],
            "base_curated_rows": base["totals"]["N_curated"],
            "rules": RULES,
            "by_rule_month": by_rule,
            "excluded_curated_rows": total,
            "working_curated_rows": base["totals"]["N_curated"] - total,
            "source_accounting_unchanged": True,
            "policy_days": len(days),
            "output_sha256": output_hashes,
            "code_sha256": sha256(Path(__file__)),
            "runtime": {
                "elapsed_seconds": round(time.monotonic() - start, 3),
                "peak_rss_mb": round(
                    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**2, 1
                ),
            },
            "limitations": [
                "The excluded intervals were selected after inspecting the time series; "
                "the affected 2025 test slice is no longer blind.",
                "A high raw-alarm share does not establish an export error or a false alarm.",
                "The overlay is a training eligibility proposal, not a physical-fault label.",
                "No other anomaly is claimed to be removed.",
            ],
        }
        (partial / "manifest.json").write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        con.close()
        shutil.rmtree(partial / "spill", ignore_errors=True)
        partial.rename(output)
        return report
    except Exception:
        con.close()
        (partial / "FAILED").write_text("Exclusion overlay incomplete; not published.\n")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.snapshot, args.output)
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("excluded_curated_rows", "working_curated_rows", "runtime")
            }
        )
    )


if __name__ == "__main__":
    main()

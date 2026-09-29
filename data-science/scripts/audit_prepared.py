"""Aggregate quality of a prepared event snapshot without loading all rows into RAM."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import duckdb


def _quote(value: Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(snapshot: Path, output: Path, summary_output: Path | None = None) -> dict:
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='4GB'")
    con.execute(f"SET temp_directory={_quote(output.parent / 'audit-spill')}")
    events = snapshot / "curated/month=*/events.parquet"
    rows = con.execute(f"""
        WITH per_channel AS (
          SELECT month, coalesce(sensor_type, '<unknown>') sensor_type, channel_id,
                 count(*) records, count(*) FILTER (WHERE alarm) alarms,
                 count(*) FILTER (WHERE reference_status = 'unmatched') unmatched,
                 count(*) FILTER (WHERE is_epoch_placeholder) epoch,
                 count(*) FILTER (WHERE value_raw IS NULL) value_null,
                 count(*) FILTER (WHERE value_raw = '') value_empty,
                 count(*) FILTER (WHERE value_numeric IS NOT NULL) numeric_values,
                 count(*) FILTER (WHERE sentinel_candidate) sentinel_candidates,
                 count(*) FILTER (WHERE value_raw = 'Обнаружен газ') registered_gas_messages,
                 count(*) FILTER (WHERE ts_group_size > 1) tied_timestamp_rows
          FROM read_parquet({_quote(events)}, hive_partitioning=true)
          GROUP BY 1, 2, 3
        )
        SELECT month, sensor_type, sum(records) records, sum(alarms) alarms,
               count(*) channels, sum(unmatched) unmatched, sum(epoch) epoch,
               sum(value_null) value_null, sum(value_empty) value_empty,
               sum(numeric_values) numeric_values,
               sum(sentinel_candidates) sentinel_candidates,
               sum(registered_gas_messages) registered_gas_messages,
               sum(tied_timestamp_rows) tied_timestamp_rows,
               max(alarms) top_channel_alarms
        FROM per_channel GROUP BY 1, 2 ORDER BY 1, 2
    """).fetchall()
    fields = [item[0] for item in con.description]
    monthly = [dict(zip(fields, row, strict=True)) for row in rows]
    for item in monthly:
        item["alarm_rate"] = item["alarms"] / item["records"] if item["records"] else None
        item["top_channel_alarm_share"] = (
            item["top_channel_alarms"] / item["alarms"] if item["alarms"] else None
        )
    lexemes = snapshot / "lexemes/*.parquet"
    lexeme_rows = con.execute(f"""
        SELECT regexp_extract(filename, '([0-9]{{4}}-[0-9]{{2}})\\.parquet$', 1) AS lexeme_month,
               sensor_type,
               count(DISTINCT lexeme) distinct_lexemes,
               sum(records) text_records
        FROM read_parquet({_quote(lexemes)}, filename=true)
        GROUP BY 1, 2 ORDER BY 1, 2
    """).fetchall()
    july_gas = []
    for year in (2020, 2021, 2022):
        path = snapshot / f"curated/month={year}-07/events.parquet"
        records, alarms, fault_alarms = con.execute(f"""
            SELECT count(*), count(*) FILTER (WHERE alarm),
                   count(*) FILTER (WHERE alarm AND value_raw='Неисправен')
            FROM read_parquet({_quote(path)}) WHERE sensor_type='Газовый датчик'
        """).fetchone()
        july_gas.append(
            {
                "year": year,
                "gas_records": records,
                "raw_alarm_true": alarms,
                "raw_alarm_rate": alarms / records if records else None,
                "raw_alarm_true_fault_text": fault_alarms,
            }
        )
    july_regime_break = july_gas[1]["raw_alarm_rate"] > max(
        july_gas[0]["raw_alarm_rate"], july_gas[2]["raw_alarm_rate"]
    )
    gaps = con.execute(f"""
        SELECT CAST(day AS VARCHAR), source_records, policy_excluded, working_covered
        FROM read_parquet({_quote(snapshot / "coverage.parquet")})
        WHERE NOT source_covered OR policy_excluded ORDER BY day
    """).fetchall()
    con.close()
    report = {
        "snapshot_policy": manifest["exclusion_policy"],
        "source_totals": manifest["totals"],
        "source_by_year": {
            str(year): {
                key: source[key]
                for key in (
                    "N_input_records",
                    "N_accepted",
                    "N_quarantine",
                    "N_technical_headers",
                    "N_out_of_scope",
                    "N_curated",
                    "N_dedup_removed",
                )
            }
            for year, source in manifest["sources"].items()
        },
        "monthly_by_sensor": monthly,
        "lexeme_dictionary_by_month_sensor": [
            {
                "month": month,
                "sensor_type": sensor,
                "distinct_lexemes": distinct,
                "text_records": records,
            }
            for month, sensor, distinct, records in lexeme_rows
        ],
        "retained_2021_july_gas_comparison": {
            "same_month_years": july_gas,
            "raw_alarm_rate_above_both_neighbors": july_regime_break,
            "interpretation": (
                "A descriptive raw-alarm regime check, not R-2021 acceptance or "
                "proof of physical faults. A remaining discontinuity keeps 2021 "
                "training eligibility unresolved."
            ),
        },
        "snapshot_disk_bytes": sum(
            path.stat().st_size for path in snapshot.rglob("*") if path.is_file()
        ),
        "input_archive_bytes": sum(source["bytes"] for source in manifest["sources"].values()),
        "output_file_hashes": len(manifest["output_sha256"]),
        "uncovered_or_excluded_dates": [
            {
                "day": day,
                "source_records": n,
                "policy_excluded": excluded,
                "working_covered": usable,
            }
            for day, n, excluded, usable in gaps
        ],
        "interpretation": (
            "Descriptive diagnostics only. A source date with zero rows is not a "
            "healthy channel; reference membership is a current snapshot; no "
            "unexplained anomaly is automatically dropped or called a failure."
        ),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    if summary_output is not None:
        by_year = {}
        for item in monthly:
            year = item["month"][:4]
            agg = by_year.setdefault(
                year,
                {
                    "curated_rows": 0,
                    "raw_alarm_true": 0,
                    "unknown_reference_rows": 0,
                    "epoch_flagged_rows": 0,
                    "timestamp_group_multirow_survivors": 0,
                },
            )
            agg["curated_rows"] += item["records"]
            agg["raw_alarm_true"] += item["alarms"]
            agg["unknown_reference_rows"] += item["unmatched"]
            agg["epoch_flagged_rows"] += item["epoch"]
            agg["timestamp_group_multirow_survivors"] += item["tied_timestamp_rows"]
        for agg in by_year.values():
            agg["raw_alarm_rate"] = agg["raw_alarm_true"] / agg["curated_rows"]
        summary = {
            "evidence_type": "local prepared-data accounting and descriptive quality",
            "etl_version": manifest["etl_version"],
            "publication_status": manifest["publication_status"],
            "training_eligibility_2021": (
                "unresolved_retained_july_regime_break"
                if july_regime_break
                else "not_assessed_by_this_diagnostic"
            ),
            "models_fitted": 0,
            "exclusion_policy": manifest["exclusion_policy"],
            "split_reservations": manifest["split_reservations"],
            "totals": manifest["totals"],
            "source_by_year": report["source_by_year"],
            "source_archive_sha256": {
                year: source["sha256"] for year, source in manifest["sources"].items()
            },
            "manifest_sha256": _sha256(snapshot / "manifest.json"),
            "output_parquet_hashes_in_manifest": report["output_file_hashes"],
            "snapshot_disk_bytes": report["snapshot_disk_bytes"],
            "input_archive_bytes": report["input_archive_bytes"],
            "runtime": manifest["runtime"],
            "publication_recovery": manifest.get("publication_recovery"),
            "by_year_descriptive": by_year,
            "source_calendar_gaps": [
                item["day"]
                for item in report["uncovered_or_excluded_dates"]
                if not item["policy_excluded"]
            ],
            "retained_2021_july_gas_comparison": report["retained_2021_july_gas_comparison"],
            "maintenance_source_sha256": {
                item["name"]: item["sha256"]
                for item in (manifest.get("maintenance") or {}).get("sources", [])
            },
            "limitations": [
                "Raw alarm is an observed event flag, not a confirmed physical failure.",
                "No daily future target or point-in-time features were built.",
                "Maintenance schedules are unjoined 2026 plans without completion evidence.",
                "Reference object membership is a current undated snapshot.",
                "The first execution's generation peak RSS was lost when an auxiliary "
                "global-ID audit hit 4 GB; "
                "the accepted output was recovered from persisted stage "
                "with both DATA-03 equations.",
            ],
        }
        summary_output.parent.mkdir(parents=True, exist_ok=True)
        summary_output.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path)
    args = parser.parse_args()
    report = run(args.snapshot, args.output, args.summary_output)
    print(
        json.dumps(
            {
                "months_x_sensor": len(report["monthly_by_sensor"]),
                "source_totals": report["source_totals"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

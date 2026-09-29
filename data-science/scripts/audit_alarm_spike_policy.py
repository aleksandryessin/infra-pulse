"""Summarize a reversible alarm-spike overlay without private event rows."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import duckdb


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _top_alarm_texts(snapshot: Path) -> dict[str, list[dict]]:
    base = snapshot / "curated"
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='4GB'")
    result = {}
    try:
        for month, day, gas_only in (
            ("2020-02", None, False),
            ("2020-03", None, False),
            ("2021-07", "2021-07-02", True),
            ("2025-11", "2025-11-06", True),
        ):
            path = base / f"month={month}" / "events.parquet"
            condition = "sensor_type = 'Газовый датчик'" if gas_only else "true"
            if day is not None:
                condition += f" AND substr(event_ts_local_raw, 1, 10) = '{day}'"
            rows = con.execute(
                "SELECT coalesce(sensor_type, '<unknown>') sensor_type, "
                "value_raw, count(*) n FROM read_parquet(?) "
                f"WHERE alarm AND {condition} "
                "GROUP BY 1, 2 ORDER BY n DESC LIMIT 8",
                [str(path)],
            ).fetchall()
            result[day or month] = [
                {"sensor_type": sensor, "value_raw": value, "alarm_true": count}
                for sensor, value, count in rows
            ]
    finally:
        con.close()
    return result


def run(audit_path: Path, policy_path: Path, snapshot: Path, output: Path) -> dict:
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if policy["status"] != "accepted_research_overlay_not_training_approval":
        raise ValueError("policy is not a published research overlay")
    if sha256(snapshot / "manifest.json") != policy["base_manifest_sha256"]:
        raise ValueError("snapshot differs from the overlay base")
    if audit["source_totals"]["N_curated"] != policy["base_curated_rows"]:
        raise ValueError("audit and overlay use different row totals")
    monthly_all = defaultdict(lambda: [0, 0])
    monthly_gas = {}
    for row in audit["monthly_by_sensor"]:
        monthly_all[row["month"]][0] += row["records"]
        monthly_all[row["month"]][1] += row["alarms"]
        if row["sensor_type"] == "Газовый датчик":
            monthly_gas[row["month"]] = (row["records"], row["alarms"])
    comparisons = []
    for item in policy["by_rule_month"]:
        month = item["month"]
        scope = "all_sensor_types" if item["sensor_type"] is None else "gas_only"
        before_rows, before_alarms = (
            monthly_all[month] if scope == "all_sensor_types" else monthly_gas[month]
        )
        removed_rows, removed_alarms = item["excluded_rows"], item["alarm_true"]
        after_rows = before_rows - removed_rows
        after_alarms = before_alarms - removed_alarms
        if after_rows < 0 or after_alarms < 0:
            raise ValueError("overlay removes more rows than the audited month contains")
        if scope == "all_sensor_types" and after_rows != 0:
            raise ValueError("the full-month 2020 exclusion is incomplete")
        comparisons.append(
            {
                "month": month,
                "scope": scope,
                "reason": item["reason"],
                "before_rows": before_rows,
                "before_alarm_true": before_alarms,
                "before_alarm_percent": round(100 * before_alarms / before_rows, 5),
                "excluded_rows": removed_rows,
                "excluded_alarm_true": removed_alarms,
                "share_of_month_alarms_excluded": round(removed_alarms / before_alarms, 6),
                "after_rows": after_rows,
                "after_alarm_true": after_alarms,
                "after_alarm_percent": (
                    round(100 * after_alarms / after_rows, 5) if after_rows else None
                ),
            }
        )
    months_2026 = [
        (month, rows, alarms, 100 * alarms / rows)
        for month, (rows, alarms) in monthly_gas.items()
        if month.startswith("2026-")
    ]
    max_2026 = max(months_2026, key=lambda item: item[3])
    highlighted_channel_concentration = [
        {
            "month": row["month"],
            "sensor_type": row["sensor_type"] or "<unknown>",
            "alarm_true": row["alarms"],
            "top_channel_alarm_share": row["top_channel_alarm_share"],
        }
        for row in audit["monthly_by_sensor"]
        if (row["month"], row["sensor_type"])
        in {
            ("2020-02", "Состояние вентилятора"),
            ("2020-03", "Датчик движения"),
            ("2021-07", "Газовый датчик"),
            ("2025-11", "Газовый датчик"),
        }
    ]
    report = {
        "policy_version": policy["policy_version"],
        "policy_manifest_sha256": sha256(policy_path),
        "base_manifest_sha256": policy["base_manifest_sha256"],
        "audit_sha256": sha256(audit_path),
        "excluded_curated_rows": policy["excluded_curated_rows"],
        "working_curated_rows": policy["working_curated_rows"],
        "comparison_by_month_scope": comparisons,
        "top_alarm_texts_by_period": _top_alarm_texts(snapshot),
        "highlighted_channel_concentration": highlighted_channel_concentration,
        "gas_2026_max_month": {
            "month": max_2026[0],
            "records": max_2026[1],
            "alarm_true": max_2026[2],
            "alarm_percent": round(max_2026[3], 5),
        },
        "evaluation_status": {
            "test_2025": "outcome_inspected_and_exclusion_selected_not_blind",
            "test_2026": "monthly_outcome_distribution_inspected_not_untouched",
        },
        "interpretation": [
            "The tall gas point near the 2026 tick is November 2025, not a 2026 month.",
            "The 2020 February-March rise mixes sensor types and unknown current references; "
            "a high raw alarm rate alone does not prove bad data.",
            "The July 2021 gas anomaly is concentrated on 2 July; retaining the "
            "rest of 2021 is still not approved for training.",
            "November 2025 was in a reserved test slice and was inspected before this "
            "policy. That slice cannot serve as an untouched final holdout.",
            "The 2026 monthly outcome distribution was also inspected; no exclusion "
            "boundary was selected in that period.",
            "This report describes event-level alarm shares; it does not define a "
            "next-day target or physical failure outcome.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--policy-manifest", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.audit, args.policy_manifest, args.snapshot, args.output)
    print(
        json.dumps(
            {
                "excluded_curated_rows": report["excluded_curated_rows"],
                "gas_2026_max_month": report["gas_2026_max_month"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

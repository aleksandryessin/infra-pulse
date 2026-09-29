"""Inventory exact dispatcher-relevant lexemes on accepted curated data.

This is descriptive evidence, not an operational alarm policy or a model gate.
The report contains aggregate counts only; no channel or object identifiers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

VERSION = "dispatch-signal-inventory-v1"
YEARS = (2023, 2024, 2025, 2026)
WATCH_LEXEMES = (
    "Батарея неисправна",
    "Батарея разряжена",
    "Вызов",
    "Выключен",
    "Затоплен",
    "Много неисправных устройств",
    "Не замкнут",
    "Неисправен",
    "Не определено",
    "Неопределен",
    "Обесточен",
    "Обнаружен газ",
    "Обнаружен дым",
    "Обнаружено движение",
    "Отключено устройство",
    "Питание от батарей",
    "Работают все насосы в АНС",
    "Рычаг сдернут",
    "Температура выше 40ºC",
    "Температура ниже 3ºC",
)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(snapshot: Path, year: int = 2025) -> dict:
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("publication_status") != "accepted":
        raise ValueError("curated snapshot is not accepted")
    paths = sorted((snapshot / "curated").glob(f"month={year}-*/events.parquet"))
    if len(paths) != 12:
        raise ValueError(f"expected 12 curated months for {year}, got {len(paths)}")
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='4GB'")
    placeholders = ", ".join("?" for _ in WATCH_LEXEMES)
    rows = con.execute(
        f"""SELECT sensor_type, value_raw, alarm,
               count(*) AS records,
               count(DISTINCT channel_id) AS channels,
               count(DISTINCT object_id) AS objects,
               count(DISTINCT (channel_id,
                   CAST(event_ts_utc + INTERVAL '3 hours' AS DATE))) AS channel_days
            FROM read_parquet(?)
            WHERE NOT is_epoch_placeholder AND value_raw IN ({placeholders})
            GROUP BY sensor_type, value_raw, alarm
            ORDER BY sensor_type, value_raw, alarm""",
        [[str(path) for path in paths], *WATCH_LEXEMES],
    ).fetchall()
    con.close()

    earlier = Counter()
    for yr in YEARS:
        lexeme_paths = sorted((snapshot / "lexemes").glob(f"{yr}-*.parquet"))
        if not lexeme_paths:
            raise ValueError(f"no lexeme summary for {yr}")
        for path in lexeme_paths:
            table = pq.read_table(path, columns=["sensor_type", "lexeme", "records"])
            for row in table.to_pylist():
                if row["lexeme"] in WATCH_LEXEMES:
                    earlier[yr, row["sensor_type"], row["lexeme"]] += int(row["records"])

    report = {
        "version": VERSION,
        "status": "descriptive_inventory_not_policy_or_model_gate",
        "models_fitted": 0,
        "code_sha256": sha256(Path(__file__)),
        "snapshot_manifest_sha256": sha256(manifest_path),
        "snapshot_etl_version": manifest["etl_version"],
        "snapshot_mapping_version": manifest["mapping_version"],
        "snapshot_timezone_assumption": manifest["timezone_assumption"],
        "year_detailed": year,
        "months_detailed": len(paths),
        "years_lexeme_summary": list(YEARS),
        "scope": "exact_raw_lexemes; counts_are_records_not_incidents_or_episodes",
        "unknown_semantics": (
            "raw text and alarm do not establish cause, physical fault, or current duration"
        ),
        "detailed": [
            {
                "sensor_type": sensor_type or "<unmatched channel>",
                "value_raw": value_raw,
                "alarm": bool(alarm),
                "records": int(records),
                "distinct_channels": int(channels),
                "distinct_objects": int(objects),
                "distinct_channel_days": int(channel_days),
            }
            for sensor_type, value_raw, alarm, records, channels, objects, channel_days in rows
        ],
        "lexeme_summary": [
            {
                "year": yr,
                "sensor_type": sensor_type or "<unmatched channel>",
                "value_raw": lexeme,
                "records": count,
            }
            for (yr, sensor_type, lexeme), count in sorted(
                earlier.items(), key=lambda item: (item[0][0], item[0][1] or "", item[0][2])
            )
        ],
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--year", type=int, default=2025)
    args = parser.parse_args()
    report = inventory(args.snapshot, args.year)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "year": report["year_detailed"],
                "rows": len(report["detailed"]),
                "summary_rows": len(report["lexeme_summary"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

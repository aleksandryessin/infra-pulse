"""Preserve the state catalogue as exact-text candidates, never as event labels."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

HEADER = ["тип_датчика", "ид_набор_состояний", "название_состояния", "тревожное"]


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(source: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError(output)
    source_hash = sha256(source)
    with source.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != HEADER:
            raise ValueError("state catalogue header mismatch")
        rows = []
        for source_row, row in enumerate(reader, 2):
            if row["тревожное"] not in {"true", "false"}:
                raise ValueError(f"invalid reference alarm at source row {source_row}")
            rows.append(
                {
                    "source_sha256": source_hash,
                    "source_row": source_row,
                    "sensor_type": row["тип_датчика"],
                    "state_set_id": row["ид_набор_состояний"],
                    "state_name": row["название_состояния"],
                    "reference_alarm": row["тревожное"] == "true",
                }
            )
    if not rows:
        raise ValueError("empty state catalogue")
    groups = defaultdict(list)
    for row in rows:
        groups[(row["sensor_type"], row["state_name"])].append(row)
    candidates = []
    for (sensor_type, state_name), matches in sorted(groups.items()):
        alarm_values = {row["reference_alarm"] for row in matches}
        set_ids = {row["state_set_id"] for row in matches}
        candidates.append(
            {
                "sensor_type": sensor_type,
                "state_name": state_name,
                "candidate_rows": len(matches),
                "candidate_set_count": len(set_ids),
                "reference_alarm_consensus": next(iter(alarm_values))
                if len(alarm_values) == 1
                else None,
                "reference_alarm_conflict": len(alarm_values) > 1,
                "channel_to_state_set_known": False,
                "historical_validity_known": False,
            }
        )
    output.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(rows), output / "source_rows.parquet", compression="zstd")
    pq.write_table(
        pa.Table.from_pylist(candidates),
        output / "exact_text_candidates.parquet",
        compression="zstd",
    )
    report = {
        "status": "diagnostic_only",
        "source_name": source.name,
        "source_sha256": source_hash,
        "source_rows": len(rows),
        "exact_text_pairs": len(candidates),
        "multi_set_pairs": sum(row["candidate_set_count"] > 1 for row in candidates),
        "conflicting_alarm_pairs": sum(row["reference_alarm_conflict"] for row in candidates),
        "usable_as_event_alarm_replacement": False,
        "usable_as_verified_target": False,
        "output_sha256": {
            name: sha256(output / name)
            for name in ("source_rows.parquet", "exact_text_candidates.parquet")
        },
    }
    (output / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.source, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()

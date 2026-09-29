"""Audit the optional state catalogue without changing accepted raw ETL semantics."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import resource
import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_HEADER = (
    "тип_датчика",
    "ид_набор_состояний",
    "название_состояния",
    "тревожное",
)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def audit(catalogue: Path, snapshot: Path, output: Path) -> dict:
    started = time.monotonic()
    with catalogue.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != EXPECTED_HEADER:
            raise ValueError("unexpected state catalogue schema")
        states = list(reader)
    if any(set(row) != set(EXPECTED_HEADER) or None in row.values() for row in states):
        raise ValueError("malformed state catalogue row")
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["publication_status"] != "accepted":
        raise ValueError("curated snapshot is not accepted")

    state_keys = {(row["тип_датчика"], row["название_состояния"]) for row in states}
    full_rows = Counter(tuple(row[field] for field in EXPECTED_HEADER) for row in states)
    alarms_by_key: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in states:
        alarms_by_key[
            (row["тип_датчика"], row["ид_набор_состояний"], row["название_состояния"])
        ].add(row["тревожное"])

    text_by_type = Counter()
    covered_by_type = Counter()
    gas_target_records = 0
    for path in sorted((snapshot / "lexemes").glob("*.parquet")):
        table = pq.read_table(path, columns=["sensor_type", "lexeme", "records"])
        for row in table.to_pylist():
            sensor_type = row["sensor_type"]
            count = row["records"]
            text_by_type[sensor_type] += count
            if (sensor_type, row["lexeme"]) in state_keys:
                covered_by_type[sensor_type] += count
            if sensor_type == "Газовый датчик" and row["lexeme"] == "Обнаружен газ":
                gas_target_records += count

    gas_type = "Газовый датчик"
    gas_lexemes_present = {
        row["название_состояния"] for row in states if row["тип_датчика"] == gas_type
    }
    report = {
        "version": "state-reference-audit-v1",
        "models_fitted": 0,
        "catalogue_sha256": sha256(catalogue),
        "curated_manifest_sha256": sha256(manifest_path),
        "dataset_sha256": hashlib.sha256(
            json.dumps(manifest["sources"], sort_keys=True).encode()
        ).hexdigest(),
        "config_sha256": manifest["config_sha256"],
        "etl_code_sha256": manifest["code_sha256"],
        "code_sha256": sha256(Path(__file__)),
        "git_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "git_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
        ),
        "catalogue": {
            "rows": len(states),
            "columns": list(EXPECTED_HEADER),
            "sensor_types": len({row["тип_датчика"] for row in states}),
            "state_sets": len({row["ид_набор_состояний"] for row in states}),
            "exact_duplicate_rows": sum(count - 1 for count in full_rows.values()),
            "conflicting_alarm_keys": sum(len(flags) > 1 for flags in alarms_by_key.values()),
            "has_channel_key": False,
            "has_effective_dates": False,
            "has_numeric_units": False,
            "has_numeric_thresholds": False,
        },
        "curated_text_coverage": {
            "all_text_records": sum(text_by_type.values()),
            "all_text_records_with_type_lexeme_match": sum(covered_by_type.values()),
            "gas_text_records": text_by_type[gas_type],
            "gas_text_records_with_type_lexeme_match": covered_by_type[gas_type],
            "gas_target_records": gas_target_records,
            "gas_target_lexeme_in_catalogue": "Обнаружен газ" in gas_lexemes_present,
            "gas_fault_lexeme_in_catalogue": "Неисправен" in gas_lexemes_present,
            "gas_unknown_lexeme_in_catalogue": "Неопределен" in gas_lexemes_present,
        },
        "coverage_semantics": "potential type+lexeme match; no channel-to-set join key",
        "conclusion": "partial_text_catalogue_no_automatic_remapping",
        "runtime": {
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**2, 1),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalogue", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.catalogue, args.snapshot, args.output)
    print(json.dumps(report["curated_text_coverage"], ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

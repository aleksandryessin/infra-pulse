"""Read-only source audit. CSV counts use parsed records, never physical line counts.

Default: fully inspect reference tables + example; inventory 7z without extracting.
Optional --archive YEAR: stream one full archive via bsdtar; keeps bounded aggregates.
The report contains no source rows, channel names or absolute source paths.
"""

import argparse
import csv
import hashlib
import json
import subprocess
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from infra_pulse_research.data.audit import profile_events


@contextmanager
def archive_stream(path: Path):
    # Only the expected CSV member is streamed. Never extract arbitrary archive paths.
    member = path.stem + ".csv"
    with subprocess.Popen(
        ["bsdtar", "-xOf", str(path), member], stdout=subprocess.PIPE, text=True, encoding="utf-8"
    ) as process:
        assert process.stdout is not None
        yield process.stdout
        code = process.wait()
        if code:
            raise RuntimeError(f"archive reader failed: {path.name} ({code})")


def profile(source: Path, archive: str | None = None) -> dict:
    channel_path = source / "справочник_каналов_датчиков.csv"
    object_path = source / "справочник_объектов_диспетчер.csv"
    with channel_path.open(encoding="utf-8-sig", newline="") as stream:
        channels = list(csv.DictReader(stream))
    with object_path.open(encoding="utf-8-sig", newline="") as stream:
        objects = list(csv.DictReader(stream))
    channel_ids = [row["ид_канала_данных"] for row in channels]
    types = {row["ид_канала_данных"]: row["тип_датчика"] for row in channels}
    object_ids = {row["ид_объект"] for row in objects}
    roots = {"", "0"}
    sources = []
    for path in sorted(source.iterdir()):
        if path.is_file() and path.suffix in {".csv", ".7z"}:
            with path.open("rb") as stream:
                checksum = hashlib.file_digest(stream, "sha256").hexdigest()
            sources.append({"name": path.name, "bytes": path.stat().st_size, "sha256": checksum})
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "scope": "full reference CSVs and example; archive content only when requested",
        "sources": sources,
        "channels": {
            "rows": len(channels),
            "distinct_ids": len(set(channel_ids)),
            "duplicate_ids": len(channel_ids) - len(set(channel_ids)),
            "null_cells": sum(not value.strip() for row in channels for value in row.values()),
            "systems": dict(Counter(row["тип_инж_системы"] for row in channels)),
            "sensor_types": dict(Counter(types.values())),
        },
        "objects": {
            "rows": len(objects),
            "distinct_ids": len(object_ids),
            "unresolved_parent_ids": sorted(
                {row["родитель"] for row in objects} - object_ids - roots
            ),
            "columns": list(objects[0]) if objects else [],
        },
        "limitations": [
            "No confirmed ODS/repair outcomes or sensor-to-object mapping supplied in these CSVs.",
            "Alarm/state record counts are not counts of independent incidents.",
            "Full archive duplicate event IDs are not checked by this bounded-memory profiler.",
            "Input timestamps have no timezone; "
            "MSK needs domain confirmation before UTC conversion.",
        ],
    }
    example = source / "журнал_событий_пример.csv"
    with example.open(encoding="utf-8-sig", newline="") as stream:
        report["example"] = profile_events(stream, types, check_ids=True)
    if archive is not None:
        path = source / f"ext-journal-{archive}.7z"
        with archive_stream(path) as stream:
            report["archive"] = {"name": path.name, **profile_events(stream, types)}
    report["completed_at"] = datetime.now(UTC).isoformat()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--archive", choices=[str(year) for year in range(2019, 2027)])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.source.is_dir():
        parser.error("source must be an existing dataset directory")
    if args.output.resolve().is_relative_to(args.source.resolve()):
        parser.error("write the report outside the source directory")
    report = profile(args.source, args.archive)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Profile saved: {args.output}")
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "sources"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

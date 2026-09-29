"""Read the two bounded 2026 planning workbooks without guessing object IDs.

Only planned work is represented. The output is research metadata, never a
confirmed repair, target, or point-in-time model feature.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pyarrow as pa
import pyarrow.parquet as pq

NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
MAX_BYTES = 5_000_000
MAX_UNCOMPRESSED_BYTES = 20_000_000
MONTH_COLUMNS = "GHIJKLMNOPQR"
MARKERS = {"ТО", "ТР", "ТО+ТР"}


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _position(address: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]+)([0-9]+)", address)
    if match is None:
        raise ValueError(f"invalid cell address: {address}")
    col = 0
    for letter in match[1]:
        col = col * 26 + ord(letter) - ord("A") + 1
    return col, int(match[2])


class Sheet:
    def __init__(self, path: Path):
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("maintenance workbook exceeds 5 MB limit")
        with zipfile.ZipFile(path) as archive:
            if sum(item.file_size for item in archive.infolist()) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("maintenance workbook exceeds 20 MB uncompressed limit")
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            sheets = workbook.findall("x:sheets/x:sheet", NS)
            if len(sheets) != 1:
                raise ValueError("expected one worksheet")
            self.name = sheets[0].attrib["name"]
            strings = []
            if "xl/sharedStrings.xml" in archive.namelist():
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                strings = [
                    "".join(t.text or "" for t in item.findall(".//x:t", NS))
                    for item in root.findall("x:si", NS)
                ]
            root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        self.cells: dict[str, object] = {}
        for cell in root.findall(".//x:sheetData/x:row/x:c", NS):
            if cell.find("x:f", NS) is not None:
                raise ValueError("formulas are unsupported in maintenance input")
            address = cell.attrib["r"]
            kind = cell.attrib.get("t")
            value = cell.findtext("x:v", default=None, namespaces=NS)
            if kind == "inlineStr":
                value = "".join(t.text or "" for t in cell.findall(".//x:t", NS))
            elif kind == "s" and value is not None:
                value = strings[int(value)]
            elif kind in (None, "n") and value is not None:
                number = float(value)
                value = int(number) if number.is_integer() else number
            if value is not None:
                self.cells[address] = value
        self.merges = []
        for merge in root.findall(".//x:mergeCells/x:mergeCell", NS):
            left, right = merge.attrib["ref"].split(":")
            self.merges.append((left, _position(left), _position(right)))

    def get(self, address: str) -> tuple[object | None, str]:
        if address in self.cells:
            return self.cells[address], address
        col, row = _position(address)
        for anchor, (c1, r1), (c2, r2) in self.merges:
            if c1 <= col <= c2 and r1 <= row <= r2:
                return self.cells.get(anchor), anchor
        return None, address


def _excel_date(value: object | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (float, int)):
        raise ValueError(f"expected Excel serial date, got {value!r}")
    return (dt.date(1899, 12, 30) + dt.timedelta(days=float(value))).isoformat()


def ppr_rows(sheet: Sheet, source_hash: str) -> list[dict]:
    rows = []
    for row in range(10, 100):
        label, _ = sheet.get(f"C{row}")
        if not isinstance(label, str) or not re.fullmatch(r"Объект [0-9]+", label.strip()):
            continue
        count, _ = sheet.get(f"D{row}")
        send_raw, send_cell = sheet.get(f"F{row}")
        send_match = re.search(r"([0-9]{2})\.([0-9]{2})\.([0-9]{4})", str(send_raw))
        send_date = (
            dt.date(int(send_match[3]), int(send_match[2]), int(send_match[1])).isoformat()
            if send_match
            else None
        )
        dates = {}
        source_cells = {"object": f"C{row}", "count": f"D{row}", "send": send_cell}
        for key, col in (("dismantle_date", "E"), ("return_date", "G"), ("commission_date", "H")):
            raw, origin = sheet.get(f"{col}{row}")
            dates[key] = _excel_date(raw)
            source_cells[key] = origin
        rows.append(
            {
                "source_sha256": source_hash,
                "sheet": sheet.name,
                "source_row": row,
                "source_cells_json": json.dumps(source_cells, ensure_ascii=False, sort_keys=True),
                "schedule_year": 2026,
                "status": "planned",
                "date_precision": "day",
                "object_label": label.strip(),
                "object_number": int(label.split()[-1]),
                "object_id": None,
                "sensor_count": int(count),
                "sensor_unit": "шт.",
                "send_raw": str(send_raw) if send_raw is not None else None,
                "send_date": send_date,
                **dates,
            }
        )
    if len(rows) != 26:
        raise ValueError(f"PPR expected 26 objects, found {len(rows)}")
    return rows


def to_rows(sheet: Sheet, source_hash: str) -> tuple[list[dict], int]:
    rows = []
    current_number = None
    current_label = None
    groups = set()
    for row in range(6, 177):
        number, _ = sheet.get(f"B{row}")
        if isinstance(number, int) and 1 <= number <= 24:
            current_number = number
            current_label, _ = sheet.get(f"C{row}")
            groups.add(number)
        equipment, _ = sheet.get(f"D{row}")
        if not isinstance(equipment, str) or equipment.strip() == "Марка" or current_number is None:
            continue
        count, _ = sheet.get(f"E{row}")
        unit, _ = sheet.get(f"F{row}")
        for month, col in enumerate(MONTH_COLUMNS, 1):
            marker, origin = sheet.get(f"{col}{row}")
            if marker is None:
                continue
            if str(marker).strip() not in MARKERS:
                raise ValueError(f"unexpected maintenance marker at {origin}: {marker!r}")
            first = dt.date(2026, month, 1)
            end = dt.date(2027, 1, 1) if month == 12 else dt.date(2026, month + 1, 1)
            rows.append(
                {
                    "source_sha256": source_hash,
                    "sheet": sheet.name,
                    "source_cell": origin,
                    "schedule_year": 2026,
                    "year_conflict": sheet.name == "2025 год",
                    "status": "planned",
                    "date_precision": "month",
                    "object_number": current_number,
                    "object_label": str(current_label),
                    "object_id": None,
                    "equipment": equipment.strip(),
                    "equipment_count": float(count) if isinstance(count, (int, float)) else None,
                    "unit": str(unit).strip() if unit is not None else None,
                    "work_kind": str(marker).strip(),
                    "month": month,
                    "period_start": first.isoformat(),
                    "period_end_exclusive": end.isoformat(),
                }
            )
    if len(groups) != 24:
        raise ValueError(f"TO expected 24 object sections, found {len(groups)}")
    return rows, len(groups)


def run(ppr_path: Path, to_path: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError(output)
    ppr_hash, to_hash = sha256(ppr_path), sha256(to_path)
    ppr_sheet, to_sheet = Sheet(ppr_path), Sheet(to_path)
    ppr = ppr_rows(ppr_sheet, ppr_hash)
    to, to_groups = to_rows(to_sheet, to_hash)
    ppr_counts = {row["object_number"]: row["sensor_count"] for row in ppr}
    gas_counts = {
        row["object_number"]: row["equipment_count"]
        for row in to
        if row["equipment"] == "Газоанализаторы"
    }
    nominal_count_conflicts = sum(
        ppr_counts.get(number) != count for number, count in gas_counts.items()
    )
    output.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(ppr), output / "ppr_2026.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(to), output / "to_2026.parquet", compression="zstd")
    manifest = {
        "status": "planned_only",
        "object_mapping": "unknown",
        "historical_availability": "unknown",
        "usable_as_target": False,
        "usable_as_feature": False,
        "sources": [
            {
                "name": ppr_path.name,
                "sha256": ppr_hash,
                "bytes": ppr_path.stat().st_size,
                "sheet": ppr_sheet.name,
            },
            {
                "name": to_path.name,
                "sha256": to_hash,
                "bytes": to_path.stat().st_size,
                "sheet": to_sheet.name,
                "year_conflict": to_sheet.name == "2025 год",
            },
        ],
        "ppr_objects": len(ppr),
        "to_objects": to_groups,
        "to_monthly_rows": len(to),
        "nominal_gas_count_comparisons": len(gas_counts),
        "nominal_gas_count_conflicts": nominal_count_conflicts,
        "nominal_count_interpretation": (
            "Same object numbers are not verified identities; a count mismatch "
            "is a warning, not a crosswalk or evidence of completed work."
        ),
        "output_sha256": {
            name: sha256(output / name) for name in ("ppr_2026.parquet", "to_2026.parquet")
        },
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ppr", type=Path, required=True)
    parser.add_argument("--to", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.ppr, args.to, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Synthetic checks for the partial-2021 policy and bounded XLSX schedules."""

import csv
import datetime as dt
import hashlib
import importlib.util
import json
import zipfile
from html import escape
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def workbook(path, sheet_name, cells, merges=()):
    xml_cells = []
    for address, value in cells.items():
        if isinstance(value, (int, float)):
            xml_cells.append(f'<c r="{address}"><v>{value}</v></c>')
        else:
            xml_cells.append(f'<c r="{address}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
    merged = "".join(f'<mergeCell ref="{item}"/>' for item in merges)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheets><sheet name="{escape(sheet_name)}" sheetId="1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData><row>{''.join(xml_cells)}</row></sheetData>"
            f"<mergeCells>{merged}</mergeCells></worksheet>",
        )


def test_maintenance_merges_month_precision_and_no_object_guess(tmp_path):
    maintenance = load("prepare_maintenance")
    ppr = tmp_path / "ppr.xlsx"
    to = tmp_path / "to.xlsx"
    ppr_cells = {}
    for number in range(1, 27):
        row = number + 9
        ppr_cells[f"C{row}"] = f"Объект {number}"
        ppr_cells[f"D{row}"] = number
    ppr_cells.update({"E10": 46034, "F10": "до 9 13.01.2026", "G10": 46044, "H10": 46049})
    workbook(ppr, "РЭК", ppr_cells, ["E10:E11", "F10:F11", "G10:G11"])
    to_cells = {}
    for number in range(1, 25):
        row = number + 5
        to_cells.update(
            {
                f"B{row}": number,
                f"C{row}": "Объект",
                f"D{row}": "Газоанализаторы",
                f"E{row}": 99 if number == 2 else number,
                f"F{row}": "шт.",
                f"G{row}": "ТО+ТР",
            }
        )
    workbook(to, "2025 год", to_cells)
    report = maintenance.run(ppr, to, tmp_path / "out")
    assert report["ppr_objects"] == 26
    assert report["to_objects"] == 24
    assert report["to_monthly_rows"] == 24
    assert report["nominal_gas_count_conflicts"] == 1
    assert report["sources"][1]["year_conflict"] is True
    ppr_rows = pq.read_table(tmp_path / "out/ppr_2026.parquet").to_pylist()
    assert ppr_rows[0]["dismantle_date"] == ppr_rows[1]["dismantle_date"]
    assert json.loads(ppr_rows[1]["source_cells_json"])["dismantle_date"] == "E10"
    assert all(row["object_id"] is None for row in ppr_rows)
    to_rows = pq.read_table(tmp_path / "out/to_2026.parquet").to_pylist()
    assert all(row["date_precision"] == "month" for row in to_rows)


def test_april_june_2021_excluded_and_sequence_split(tmp_path):
    curate = load("curate_journal")
    source = tmp_path / "source"
    source.mkdir()
    with (source / "справочник_каналов_датчиков.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "ид_канала_данных",
                "тип_инж_системы",
                "тип_датчика",
                "тег_инженерной_системы",
                "название_датчика",
                "ид_объект",
            ]
        )
        writer.writerow([1, "Газовая охрана", "Газовый датчик", "1-1.1.1.", "ПК1", 10])
    with (source / "справочник_объектов_диспетчер.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["ид_объект", "иерархия_уровень", "родитель"])
        writer.writerow([10, 3, 1])
    curate.reference(source, tmp_path)
    rows = []
    for ordinal, day in enumerate(("2021-03-31", "2021-04-01", "2021-06-30", "2021-07-01"), 1):
        rows.append(
            {
                "source_file": "ext-journal-2021.7z",
                "source_sha256": "sha",
                "record_ordinal": ordinal,
                "event_id_raw": str(ordinal),
                "channel_id_raw": "1",
                "date_raw": day,
                "time_raw": "12:00:00",
                "alarm_raw": "f",
                "value_raw": "Норма",
                "column_count": 6,
                "raw_cells_json": "[]",
            }
        )
    curate.flush_stage(tmp_path / "staging", "2021-04", 0, rows)
    result = curate.process_bucket(
        duckdb.connect(),
        tmp_path,
        "2021-04",
        "2026-09-25T00:00:00Z",
        "synthetic-ref",
        "exclude-2021-apr-jun-v1",
    )
    assert result["N_input_records"] == 4
    assert result["N_out_of_scope"] == 2
    assert result["N_curated"] == 2
    events = pq.read_table(tmp_path / "curated/month=2021-04/events.parquet").to_pylist()
    assert {item["sequence_segment"] for item in events} == {"pre-2021", "post-2021"}
    assert all(item["exclusion_policy_version"] == "exclude-2021-apr-jun-v1" for item in events)
    coverage = curate.build_coverage(
        {day: 1 for day in ("2021-03-31", "2021-04-01", "2021-06-30", "2021-07-01")},
        tmp_path,
        [2021],
        "exclude-2021-apr-jun-v1",
    )
    assert coverage["calendar_rows"] == 93
    days = {
        str(row["day"]): row for row in pq.read_table(tmp_path / "coverage.parquet").to_pylist()
    }
    assert days["2021-04-01"]["policy_excluded"] is True
    assert days["2021-07-01"]["working_covered"] is True


def test_prepared_view_reserves_splits_preserves_null_and_gap(tmp_path):
    curate = load("curate_journal")
    prepared_path = (
        Path(__file__).resolve().parents[1] / "src/infra_pulse_research/data/prepared.py"
    )
    spec = importlib.util.spec_from_file_location("prepared", prepared_path)
    prepared = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepared)
    snapshot = tmp_path / "snapshot"
    folder = snapshot / "curated/month=2026-06"
    folder.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "event_ts_local_raw": "2025-06-30 23:59:59",
                    "value_raw": None,
                    "alarm": None,
                    "is_epoch_placeholder": False,
                },
                {
                    "event_ts_local_raw": "2025-07-01 00:00:00",
                    "value_raw": "Норма",
                    "alarm": False,
                    "is_epoch_placeholder": False,
                },
                {
                    "event_ts_local_raw": "2026-06-02 00:00:00",
                    "value_raw": "01.01.1970 03:00:01",
                    "alarm": True,
                    "is_epoch_placeholder": True,
                },
            ]
        ),
        folder / "events.parquet",
    )
    (snapshot / "manifest.json").write_text(
        json.dumps({"publication_status": "accepted", "split_reservations": curate.SPLITS})
    )
    con = prepared.open_prepared(snapshot)
    result = con.execute(
        "SELECT value_raw, alarm, split_reservation FROM prepared_events "
        "ORDER BY event_ts_local_raw"
    ).fetchall()
    assert result == [
        (None, None, "validation"),
        ("Норма", False, "test_2025"),
        ("01.01.1970 03:00:01", True, "temporal_unusable"),
    ]
    con.close()
    curate.build_coverage(
        {"2026-05-31": 1, "2026-06-02": 1},
        snapshot,
        [2026],
        "exclude-2021-apr-jun-v1",
    )
    calendar = pq.read_table(snapshot / "coverage.parquet").to_pylist()
    gap = next(row for row in calendar if str(row["day"]) == "2026-06-01")
    assert gap["source_covered"] is False
    assert gap["working_covered"] is False


def test_state_reference_remains_ambiguous_diagnostic(tmp_path):
    prepare = load("prepare_state_candidates")
    source = tmp_path / "states.csv"
    with source.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(prepare.HEADER)
        writer.writerow(["Состояние вентилятора", "16", "Норма", "false"])
        writer.writerow(["Состояние вентилятора", "17", "Норма", "true"])
        writer.writerow(["Состояние вентилятора", "17", "Норма", "true"])
    report = prepare.run(source, tmp_path / "candidates")
    assert report["source_rows"] == 3
    assert report["exact_text_pairs"] == 1
    assert report["multi_set_pairs"] == 1
    assert report["conflicting_alarm_pairs"] == 1
    row = pq.read_table(tmp_path / "candidates/exact_text_candidates.parquet").to_pylist()[0]
    assert row["reference_alarm_consensus"] is None
    assert row["channel_to_state_set_known"] is False


def test_alarm_spike_overlay_boundaries_and_sensor_scope(tmp_path):
    overlay = load("apply_alarm_spike_policy")
    assert overlay.months_touched(overlay.RULES[0]) == ["2020-02", "2020-03"]
    overlay.validate_rules(overlay.RULES)
    prepared_path = (
        Path(__file__).resolve().parents[1] / "src/infra_pulse_research/data/prepared.py"
    )
    spec = importlib.util.spec_from_file_location("prepared_overlay", prepared_path)
    prepared = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepared)
    assert prepared.window_crosses_policy_exclusion(
        overlay.RULES,
        sensor_type="КД АВ",
        start=dt.date(2020, 1, 31),
        end=dt.date(2020, 2, 2),
    )
    assert not prepared.window_crosses_policy_exclusion(
        overlay.RULES,
        sensor_type="КД АВ",
        start=dt.date(2020, 1, 31),
        end=dt.date(2020, 2, 1),
    )
    assert prepared.window_crosses_policy_exclusion(
        overlay.RULES,
        sensor_type="Газовый датчик",
        start=dt.date(2021, 7, 2),
        end=dt.date(2021, 7, 3),
    )
    assert not prepared.window_crosses_policy_exclusion(
        overlay.RULES,
        sensor_type="КД АВ",
        start=dt.date(2021, 7, 2),
        end=dt.date(2021, 7, 3),
    )
    snapshot = tmp_path / "snapshot"
    events = snapshot / "curated/month=2020-02"
    events.mkdir(parents=True)
    samples = [
        ("2020-01-31", "КД АВ"),
        ("2020-02-01", "КД АВ"),
        ("2020-03-31", None),
        ("2020-04-01", "КД АВ"),
        ("2021-07-02", "Газовый датчик"),
        ("2021-07-02", "КД АВ"),
        ("2021-07-02", None),
        ("2025-11-06", "Газовый датчик"),
        ("2025-11-06", "КД АВ"),
        ("2025-11-07", "Газовый датчик"),
    ]
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "event_ts_local_raw": day + " 12:00:00",
                    "sensor_type": sensor,
                    "alarm": False,
                    "is_epoch_placeholder": False,
                }
                for day, sensor in samples
            ]
        ),
        events / "events.parquet",
    )
    base_manifest = snapshot / "manifest.json"
    base_manifest.write_text(
        json.dumps({"publication_status": "accepted", "split_reservations": {}})
    )
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    policy_path = policy_dir / "manifest.json"
    policy_path.write_text(
        json.dumps(
            {
                "status": "accepted_research_overlay_not_training_approval",
                "base_manifest_sha256": hashlib.sha256(base_manifest.read_bytes()).hexdigest(),
                "rules": overlay.RULES,
            }
        )
    )
    pq.write_table(
        pa.Table.from_pylist([{"day": "2025-11-06", "sensor_type_scope": "Газовый датчик"}]),
        policy_dir / "policy_days.parquet",
    )
    con = prepared.open_prepared(snapshot, policy_manifest=policy_path)
    assert con.execute("SELECT count(*) FROM working_events").fetchone()[0] == 6
    assert con.execute("SELECT count(*) FROM policy_excluded_events").fetchone()[0] == 4
    assert (
        con.execute(
            "SELECT count(*) FROM working_events WHERE sensor_type IS NULL "
            "AND event_ts_local_raw LIKE '2021-07-02%'"
        ).fetchone()[0]
        == 1
    )
    assert con.execute("SELECT count(*) FROM policy_days").fetchone()[0] == 1
    con.close()
    policy = json.loads(policy_path.read_text())
    policy["base_manifest_sha256"] = "incorrect"
    policy_path.write_text(json.dumps(policy))
    try:
        prepared.open_prepared(snapshot, policy_manifest=policy_path)
    except ValueError as error:
        assert "different prepared snapshot" in str(error)
    else:
        raise AssertionError("overlay with wrong base hash was accepted")

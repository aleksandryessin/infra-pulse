"""Synthetic invariants of raw-to-curated accounting and time semantics."""

import csv
import importlib.util
import io
import json
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/curate_journal.py"
spec = importlib.util.spec_from_file_location("curate_journal", SCRIPT)
curate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(curate)


def test_picket_forms_and_tag_reference(tmp_path):
    assert curate.picket("ТД ПК86-85") == {
        "picket_from": 85.0,
        "picket_to": 86.0,
        "picket_form": "range",
        "picket_parsed": True,
        "picket_fractional": False,
        "picket_reversed": True,
    }
    assert curate.picket("ВШ ПК88,5")["picket_from"] == 88.5
    assert curate.picket("без пикета")["picket_parsed"] is False
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
        writer.writerow(["1", "Пожарная", "Газовый датчик", "15-11.1.131.2.", "ГД ПК88,5", "20"])
    with (source / "справочник_объектов_диспетчер.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["ид_объект", "иерархия_уровень", "родитель"])
        writer.writerow(["20", "1", "0"])
    refs, counts = curate.reference(source, tmp_path)
    assert refs["channels"]["sha256"]
    assert counts == {
        "channels": 1,
        "objects_total": 1,
        "objects_referenced_by_channels": 1,
        "picket_parsed": 1,
    }
    channels = pq.read_table(tmp_path / "channels.parquet").to_pylist()
    assert channels[0]["tag_l1"] == "15"


def test_both_data03_equations_and_no_collapsing_simultaneous_values(tmp_path):
    test_picket_forms_and_tag_reference(tmp_path)
    values = [
        ("1", "1", "2023-01-01", "12:00:00", "f", "0.01"),
        ("2", "1", "2023-01-01", "12:00:00", "f", "0.01"),
        ("3", "1", "2023-01-01", "12:00:00", "t", "Обнаружен газ"),
        ("4", "999", "1970-01-01", "03:00:00", "f", "0.00"),
        ("bad", "1", "2023-01-01", "12:01:00", "f", "Норма"),
        ("6", "1", "2023-01-01", "12:02:00", "maybe", "Норма"),
        ("7", "1", "2023-01-01", "12:03:00", "f", "01.01.1970 03:00:01"),
    ]
    buckets = {}
    for ordinal, cells in enumerate(values, 1):
        key = "1970-01" if ordinal == 4 else "2023-01"
        row = dict(
            zip(
                (
                    "event_id_raw",
                    "channel_id_raw",
                    "date_raw",
                    "time_raw",
                    "alarm_raw",
                    "value_raw",
                ),
                cells,
                strict=True,
            )
        )
        row.update(
            source_file="synthetic.7z",
            source_sha256="sha",
            record_ordinal=ordinal,
            column_count=6,
            raw_cells_json=json.dumps(cells),
        )
        buckets.setdefault(key, []).append(row)
    connection = duckdb.connect()
    connection.execute("SET threads=2")
    connection.execute("SET memory_limit='4GB'")
    results = []
    for bucket, rows in buckets.items():
        curate.flush_stage(tmp_path / "staging", bucket, 0, rows)
        results.append(
            curate.process_bucket(
                connection, tmp_path, bucket, "2026-09-23T00:00:00Z", "synthetic-ref"
            )
        )
    assert sum(r["N_input_records"] for r in results) == 7
    assert sum(r["N_quarantine"] for r in results) == 2
    assert sum(r["N_accepted"] for r in results) == 5
    assert sum(r["N_curated"] for r in results) == 4
    assert sum(r["N_dedup_removed"] for r in results) == 1
    assert sum(r["n_epoch_placeholder"] for r in results) == 2
    assert sum(r["n_unmatched_rows"] for r in results) == 1
    assert sum(r["n_target_messages_in_multi_value_groups"] for r in results) == 1
    curated = pq.read_table(tmp_path / "curated/month=2023-01/events.parquet").to_pylist()
    assert len(curated) == 3
    assert {row["value_raw"] for row in curated} == {"0.01", "Обнаружен газ", "01.01.1970 03:00:01"}
    assert {row["ts_group_distinct_values"] for row in curated} == {1, 2}
    assert {str(row["event_ts_utc"]) for row in curated} == {
        "2023-01-01 09:00:00",
        "2023-01-01 09:03:00",
    }
    assert (
        next(row for row in curated if row["value_raw"].startswith("01.01.1970"))[
            "is_epoch_placeholder"
        ]
        is True
    )
    assert curated[0]["available_at"] is None
    assert all(row["source_covered"] for row in curated)
    assert {row["sequence_segment"] for row in curated} == {"post-2021"}
    assert {row["reference_version"] for row in curated} == {"synthetic-ref"}
    assert (
        next(row for row in curated if row["value_raw"] == "Обнаружен газ")["rare_lexeme"] is True
    )
    quarantine = pq.read_table(tmp_path / "quarantine/2023-01.parquet").to_pylist()
    assert {row["quarantine_reason"] for row in quarantine} == {"event_id_invalid", "alarm_invalid"}
    dedup = pq.read_table(tmp_path / "dedup/2023-01.parquet").to_pylist()
    assert dedup[0]["survivor_uid"] in {row["row_uid"] for row in curated}
    ids = curate.event_id_duplicates(connection, tmp_path)
    assert sum(row["n_event_id_duplicates"] for row in ids) == 0


def test_coverage_gap_is_explicit_and_does_not_bridge_excluded_year(tmp_path):
    counts = {"2022-12-31": 3, "2023-01-01": 4, "2023-01-03": 5}
    coverage = curate.build_coverage(counts, tmp_path, [2022, 2023])
    assert coverage["n_uncovered_dates"] == 1
    assert coverage["n_windows_gap"] == 1
    assert coverage["n_cutoffs_lookback_gap"] == 30
    calendar = pq.read_table(tmp_path / "coverage.parquet").to_pylist()
    assert (
        next(row for row in calendar if str(row["day"]) == "2023-01-02")["source_covered"] is False
    )


def test_alarm_true_false_spellings_are_boolean_not_state_labels(tmp_path):
    test_picket_forms_and_tag_reference(tmp_path)
    rows = []
    for index, (literal, value) in enumerate(
        [("true", "0.01"), ("false", "Неисправен"), ("t", "Норма"), ("f", "0.02")], 1
    ):
        rows.append(
            {
                "source_file": "synthetic.7z",
                "source_sha256": "sha",
                "record_ordinal": index,
                "event_id_raw": str(index),
                "channel_id_raw": "1",
                "date_raw": "2023-01-01",
                "time_raw": f"12:00:0{index}",
                "alarm_raw": literal,
                "value_raw": value,
                "column_count": 6,
                "raw_cells_json": "[]",
            }
        )
    curate.flush_stage(tmp_path / "staging", "2023-01", 0, rows)
    result = curate.process_bucket(
        duckdb.connect(), tmp_path, "2023-01", "2026-09-23T00:00:00Z", "synthetic-ref"
    )
    assert result["N_curated"] == 4
    table = pq.read_table(tmp_path / "curated/month=2023-01/events.parquet").to_pylist()
    assert [row["alarm"] for row in table] == [True, False, True, False]
    assert [row["state_lexeme"] for row in table] == [None, "Неисправен", "Норма", None]


def test_logical_csv_records_and_repeated_header_accounting(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "ext-journal-2025.7z").write_bytes(b"synthetic")
    rows = [
        curate.HEADER,
        ["1", "1", "2025-01-01", "00:00:00", "f", "line\nbreak"],
        curate.HEADER,
        ["2", "1", "2025-01-01", "00:00:01", "f", "Норма"],
    ]
    buffer = io.StringIO(newline="")
    csv.writer(buffer).writerows(rows)

    @contextmanager
    def fake_archive(_path):
        yield io.StringIO(buffer.getvalue(), newline="")

    monkeypatch.setattr(curate, "archive", fake_archive)
    monkeypatch.setitem(curate.EXPECTED, 2025, 2)
    sources, buckets, _coverage = curate.scan_archives(source, [2025], tmp_path, 10)
    assert sources["2025"]["N_input_records"] == 3
    assert sources["2025"]["N_technical_headers"] == 1
    assert buckets == ["2025-01"]
    staged = pq.read_table(tmp_path / "staging/2025-01/batch-000000.parquet").to_pylist()
    assert [row["record_ordinal"] for row in staged] == [1, 3]
    assert staged[0]["value_raw"] == "line\nbreak"


def test_2021_record_is_proven_out_of_scope_before_payload_validation(tmp_path):
    test_picket_forms_and_tag_reference(tmp_path)
    row = {
        "source_file": "ext-journal-2022.7z",
        "source_sha256": "sha",
        "record_ordinal": 1,
        "event_id_raw": "bad",
        "channel_id_raw": "1",
        "date_raw": "2021-12-31",
        "time_raw": "12:00:00",
        "alarm_raw": "maybe",
        "value_raw": "Норма",
        "column_count": 6,
        "raw_cells_json": '["bad", "1", "2021-12-31", "12:00:00", "maybe", "Норма"]',
    }
    curate.flush_stage(tmp_path / "staging", "2021-12", 0, [row])
    result = curate.process_bucket(
        duckdb.connect(), tmp_path, "2021-12", "2026-09-23T00:00:00Z", "synthetic-ref"
    )
    assert result["N_input_records"] == result["N_out_of_scope"] == 1
    assert result["N_quarantine"] == result["N_accepted"] == 0
    excluded = pq.read_table(tmp_path / "excluded/2021-12.parquet").to_pylist()
    assert excluded[0]["reason"] == "excluded_2021"

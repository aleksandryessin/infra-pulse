"""CSV adapter without PostgreSQL: both journal shapes, quarantine, references, 413.

All inputs are synthetic; no customer rows are used.
"""

import io
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from infra_pulse_backend.api import imports as imports_api
from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings
from infra_pulse_backend.ingestion.csv_source import FileRejected
from infra_pulse_backend.ingestion.journal_csv import (
    MSK,
    dedup_identity,
    numeric_value,
    parse_journal,
)
from infra_pulse_backend.ingestion.reference_csv import parse_reference

QUOTED_HEADER = '"ид_события","ид_канала_данных","дата","время","тревожное","значение_датчика"'
PLAIN_HEADER = "ид_события,ид_канала_данных,дата,время,тревожное,значение_датчика"


def quoted(rows: list[tuple]) -> bytes:
    """Organizers' example shape: quoted text cells, ``true``/``false``, BOM."""
    lines = [QUOTED_HEADER]
    for event_id, channel, date, time, alarm, value in rows:
        flag = "true" if alarm else "false"
        lines.append(f'{event_id},{channel},"{date}","{time}",{flag},"{value}"')
    return ("﻿" + "\n".join(lines) + "\n").encode()


def plain(rows: list[tuple]) -> bytes:
    """Archive shape: no quotes, ``t``/``f``, no BOM, CRLF line ends."""
    lines = [PLAIN_HEADER]
    for event_id, channel, date, time, alarm, value in rows:
        lines.append(f"{event_id},{channel},{date},{time},{'t' if alarm else 'f'},{value}")
    return ("\r\n".join(lines) + "\r\n").encode()


ROWS = [
    ("9000001", "700001", "2026-09-20", "03:09:27", False, "28"),
    ("9000002", "700002", "2026-09-20", "23:59:59", True, "Неисправен"),
    ("9000002", "700003", "2026-09-21", "00:00:00", False, "0.01"),
    ("9000004", "700004", "1970-01-01", "03:00:00", False, "Норма"),
    ("9000005", "700005", "2026-09-21", "10:00:00", False, "01.01.1970 03:00:01"),
]


@pytest.mark.parametrize("encode", [quoted, plain])
def test_both_journal_shapes_give_the_same_records(encode):
    parsed = parse_journal(encode(ROWS))
    assert parsed.quarantined == []
    assert [row.value_raw for row in parsed.rows] == [row[5] for row in ROWS]
    assert [row.alarm for row in parsed.rows] == [False, True, False, False, False]
    first = parsed.rows[0]
    assert first.event_at == datetime(2026, 9, 20, 0, 9, 27, tzinfo=UTC)
    assert first.event_at.utcoffset() == MSK.utcoffset(None)
    assert first.event_local_raw == "2026-09-20 03:09:27"
    assert first.value_numeric == 28.0
    assert parsed.rows[1].value_numeric is None
    assert parsed.rows[2].value_numeric == 0.01
    # ид_события is text and not unique (DATA-03).
    assert parsed.rows[1].event_id == parsed.rows[2].event_id == "9000002"
    assert [row.is_epoch_placeholder for row in parsed.rows] == [False, False, False, True, True]
    # The placeholder date does not stretch the reported event period back to 1970.
    assert parsed.period() == (first.event_at, parsed.rows[4].event_at)


def test_overlap_key_is_independent_of_quoting_and_keeps_distinct_values():
    left = parse_journal(quoted(ROWS)).rows
    right = parse_journal(plain(ROWS)).rows
    assert [dedup_identity(row) for row in left] == [dedup_identity(row) for row in right]
    changed = parse_journal(plain([(*ROWS[0][:5], "29")])).rows[0]
    assert dedup_identity(changed) != dedup_identity(left[0])
    other_event = parse_journal(plain([("9000009", *ROWS[0][1:])])).rows[0]
    assert dedup_identity(other_event) != dedup_identity(left[0])


def test_numeric_value_matches_curated_rule():
    assert numeric_value("-.5") == -0.5
    assert numeric_value("+12.25") == 12.25
    for text in ("1,5", "1e3", "nan", "inf", " 1", "", "Обнаружен дым", "9" * 400):
        assert numeric_value(text) is None


def test_quarantine_keeps_line_numbers_and_reasons():
    data = (
        "\n".join(
            [
                PLAIN_HEADER,
                "1,700001,2026-09-20,10:00:00,f,ok",  # line 2
                "2,700001,2026-09-20,10:00:00,f",  # 3 bad_column_count
                "3,700001,2026-02-30,10:00:00,f,x",  # 4 bad_date
                "4,700001,20.09.2026,10:00:00,f,x",  # 5 bad_date
                "5,700001,2026-09-20,24:00:00,f,x",  # 6 bad_time
                "6,700001,2026-09-20,10:00,f,x",  # 7 bad_time
                "7,700001,2026-09-20,10:00:00,yes,x",  # 8 bad_bool
                "8,,2026-09-20,10:00:00,f,x",  # 9 empty_channel
                '9,700001,2026-09-20,10:00:00,f,"two',  # 10 multi-line cell
                'lines"',
                f"10,700001,2026-09-20,10:00:00,f,{'x' * 5000}",  # 12 value_too_long
                "",
                PLAIN_HEADER,  # 14 repeated header, not a record
                "11,700001,2026-09-20,10:00:01.250,t,ok",  # 15
            ]
        ).encode()
        + b"\n12,700001,2026-09-20,10:00:02,f,\xff\xfe\n"
    )  # 16 bad_encoding
    parsed = parse_journal(data)
    reasons = [(item.line_no, item.reason) for item in parsed.quarantined]
    assert reasons == [
        (3, "bad_column_count"),
        (4, "bad_date"),
        (5, "bad_date"),
        (6, "bad_time"),
        (7, "bad_time"),
        (8, "bad_bool"),
        (9, "empty_channel"),
        (12, "value_too_long"),
        (16, "bad_encoding"),
    ]
    assert [(row.line_no, row.record_ordinal) for row in parsed.rows] == [(2, 1), (10, 9), (15, 11)]
    assert parsed.rows[1].value_raw == "two\nlines"
    assert parsed.rows[2].event_at.microsecond == 250000
    assert parsed.technical_headers == 1
    assert parsed.records == 12
    bad = parsed.quarantined[-1]
    assert json.loads(bad.raw_json())[-1] == "\\xff\\xfe"
    assert bad.excerpt().endswith(",\\xff\\xfe")
    assert len(parsed.quarantined[-2].excerpt()) == 200


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"", "bad_header"),
        ("ид,канал\n1,2\n".encode(), "bad_header"),
        (PLAIN_HEADER.encode("cp1251") + b"\n", "bad_encoding"),
        ("﻿".encode() + PLAIN_HEADER.encode("utf-16"), "bad_encoding"),
    ],
)
def test_file_level_rejections(data, code):
    with pytest.raises(FileRejected) as error:
        parse_journal(data)
    assert error.value.code == code


def test_nul_byte_is_quarantined_not_stored():
    parsed = parse_journal((PLAIN_HEADER + "\n1,700001,2026-09-20,10:00:00,f,a\x00b\n").encode())
    assert [item.reason for item in parsed.quarantined] == ["bad_encoding"]
    assert json.loads(parsed.quarantined[0].raw_json())[-1] == "a\\x00b"


def test_reference_parsers_keep_text_and_count_repeats():
    channels = (
        '"ид_канала_данных","тип_инж_системы","тип_датчика","тег_инженерной_системы",'
        '"название_датчика","ид_объект"\n'
        '700001,Электроснабжение,Состояние фазы,"99-1.1.1.",Фаза A ПК1,5001\n'
        '700001,Электроснабжение,Состояние фазы,"99-1.1.1.",Фаза A ПК1,5001\n'
        '700002,Электроснабжение,Состояние фазы,"99-1.1.2.",Фаза B ПК1,\n'
        ',Электроснабжение,Состояние фазы,"99-1.1.3.",Фаза C ПК1,5001\n'
    ).encode()
    parsed = parse_reference(channels, "channels")
    assert parsed.rows == [
        (2, "700001", "Электроснабжение", "Состояние фазы", "99-1.1.1.", "Фаза A ПК1", "5001"),
        (4, "700002", "Электроснабжение", "Состояние фазы", "99-1.1.2.", "Фаза B ПК1", None),
    ]
    assert parsed.duplicates == 1
    assert [(item.line_no, item.reason) for item in parsed.quarantined] == [(5, "empty_channel")]
    assert parsed.records == 4

    states = (
        "﻿"
        '"тип_датчика","ид_набор_состояний","название_состояния","тревожное"\n'
        "Состояние фазы,7,Норма,false\nСостояние фазы,7,Неисправен,t\nСостояние фазы,7,x,maybe\n"
    ).encode()
    parsed = parse_reference(states, "states")
    assert [row[1:] for row in parsed.rows] == [
        ("Состояние фазы", "7", "Норма", False),
        ("Состояние фазы", "7", "Неисправен", True),
    ]
    assert [item.reason for item in parsed.quarantined] == ["bad_bool"]

    objects = (
        '"ид_объект","иерархия_уровень","родитель","вид_объекта","диспетчерское_название_объекта"\n'
        "5001,3,5,controlHouse,Синтетический объект\n"
    ).encode()
    assert parse_reference(objects, "objects").rows == [
        (2, "5001", "3", "5", "controlHouse", "Синтетический объект")
    ]
    with pytest.raises(FileRejected):
        parse_reference(objects, "channels")


def test_upload_over_limit_is_413_before_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(imports_api, "MAX_IMPORT_BYTES", 64)
    settings = Settings(
        mode="received",
        db_dsn="postgresql://unused.invalid/none",
        upload_dir=tmp_path,
        _env_file=None,
    )
    client = TestClient(create_app(settings))
    response = client.post(
        "/api/v1/imports",
        files={"file": ("big.csv", io.BytesIO(b"x" * 65), "text/csv")},
        data={"format": "journal_csv"},
    )
    assert (response.status_code, response.json()["detail"]) == (413, "file_too_large")
    assert list(tmp_path.iterdir()) == []


def test_upload_without_database_is_503(tmp_path):
    client = TestClient(create_app(Settings(mode="received", upload_dir=tmp_path, _env_file=None)))
    response = client.post(
        "/api/v1/imports", files={"file": ("a.csv", io.BytesIO(b"x"), "text/csv")}
    )
    assert (response.status_code, response.json()["detail"]) == (503, "imports_not_configured")
    assert client.get("/api/v1/imports/imp-x").json()["detail"] == "imports_not_configured"

"""G1 without PostgreSQL: XLSX import, the ТЗ Appendix 1 journal layout (ТЗ §7, Прил. 1).

All workbooks and rows are synthetic, built in the test; the three Appendix 1 rows are
the example printed in the ТЗ (IDs without the typographic digit grouping).
"""

import io
import zipfile
from datetime import UTC, date, datetime, time

import openpyxl
import pytest

from infra_pulse_backend.ingestion import xlsx_source
from infra_pulse_backend.ingestion.channel_types import contradicts
from infra_pulse_backend.ingestion.csv_source import FileRejected
from infra_pulse_backend.ingestion.journal_csv import (
    JOURNAL_HEADER,
    TZ_JOURNAL_HEADER,
    dedup_identity,
    parse_journal,
)
from infra_pulse_backend.ingestion.reference_csv import CHANNELS_HEADER, parse_reference
from infra_pulse_backend.ingestion.xlsx_source import cell_text

ROWS = [
    ("9000001", "700001", "2026-09-20", "03:09:27", False, "28"),
    ("9000002", "700002", "2026-09-20", "23:59:59", True, "Неисправен"),
    ("9000002", "700003", "2026-09-21", "00:00:00", False, "0.01"),
    ("9000004", "700004", "1970-01-01", "03:00:00", False, "Норма"),
    ("9000005", "700005", "2026-09-21", "10:00:00", False, "01.01.1970 03:00:01"),
]
# ТЗ Appendix 1, «Журнал записей со значениями с каналов данных датчиков».
TZ_EXAMPLE = [
    ("3008235019", "56682", "12", "25,00", "19.10.2026 12:15"),
    ("3008235020", "183582", "12", "25,40", "19.10.2026 12:16"),
    ("3008235021", "215811", "12", "33,50", "19.10.2026 12:17"),
]


def csv_bytes(rows) -> bytes:
    lines = [",".join(JOURNAL_HEADER)]
    for event_id, channel, day, clock, alarm, value in rows:
        lines.append(f"{event_id},{channel},{day},{clock},{'t' if alarm else 'f'},{value}")
    return ("\n".join(lines) + "\n").encode()


def workbook(rows: list[list], *, formats: dict[tuple[int, int], str] | None = None) -> bytes:
    book = openpyxl.Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    for (row, column), number_format in (formats or {}).items():
        sheet.cell(row=row, column=column).number_format = number_format
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def typed_rows(rows) -> list[list]:
    """As Excel stores them: integer IDs, date and time cells, boolean, text values."""
    typed = [list(JOURNAL_HEADER)]
    for event_id, channel, day, clock, alarm, value in rows:
        typed.append(
            [
                int(event_id),
                int(channel),
                date.fromisoformat(day),
                time.fromisoformat(clock),
                alarm,
                value,
            ]
        )
    return typed


def test_xlsx_journal_gives_the_same_records_as_csv():
    from_csv = parse_journal(csv_bytes(ROWS))
    from_xlsx = parse_journal(workbook(typed_rows(ROWS)))
    assert from_xlsx.container == "xlsx" and from_csv.container == "csv"
    assert from_xlsx.quarantined == []
    assert [dedup_identity(row) for row in from_xlsx.rows] == [
        dedup_identity(row) for row in from_csv.rows
    ]
    # IDs are text without a float tail; the placeholder value stays verbatim.
    assert [row.event_id for row in from_xlsx.rows][:2] == ["9000001", "9000002"]
    assert from_xlsx.rows[0].channel_id == "700001"
    assert from_xlsx.rows[4].value_raw == "01.01.1970 03:00:01"
    assert [row.alarm for row in from_xlsx.rows] == [False, True, False, False, False]
    assert [row.is_epoch_placeholder for row in from_xlsx.rows] == [
        row.is_epoch_placeholder for row in from_csv.rows
    ]


def test_xlsx_all_text_cells_are_kept_verbatim():
    rows = [list(JOURNAL_HEADER), ["00017", "700001", "2026-09-20", "03:09:27", "true", "1,5"]]
    parsed = parse_journal(workbook(rows))
    assert parsed.rows[0].event_id == "00017"
    assert parsed.rows[0].value_raw == "1,5" and parsed.rows[0].value_numeric is None


def test_cell_text_rules():
    assert cell_text(56682) == "56682"
    assert cell_text(56682.0) == "56682"
    assert cell_text(3008235019.0) == "3008235019"
    assert cell_text(25.4) == "25.4"
    assert cell_text(True) == "true" and cell_text(False) == "false"
    assert cell_text(None) == ""
    assert cell_text(datetime(2026, 9, 20)) == "2026-09-20"
    assert cell_text(datetime(2026, 9, 20), "dd.mm.yyyy hh:mm") == "20.09.2026 00:00:00"
    assert cell_text(datetime(1970, 1, 1, 3, 0, 0)) == "01.01.1970 03:00:00"
    assert cell_text(time(10, 0, 1, 250000)) == "10:00:01.25"


def test_typed_placeholder_datetime_value_stays_recognisable():
    rows = typed_rows(ROWS[:1])
    rows[1][5] = datetime(1970, 1, 1, 3, 0, 0)
    parsed = parse_journal(
        workbook(rows, formats={(2, 6): "dd.mm.yyyy hh:mm:ss"}),
    )
    assert parsed.rows[0].value_raw == "01.01.1970 03:00:00"
    assert parsed.rows[0].is_epoch_placeholder


def test_xlsx_rows_quarantine_like_csv_and_skip_blank_rows():
    rows = [
        list(JOURNAL_HEADER),
        ["1", "700001", "2026-09-20", "10:00:00", "f", "ok"],  # row 2
        [],  # blank row 3: not a record
        ["2", "", "2026-09-20", "10:00:00", "f", "x"],  # row 4: empty_channel
        ["3", "700001", "2026-02-30", "10:00:00", "f", "x"],  # row 5: bad_date
        ["4", "700001", "2026-09-20", "10:00:00", "f", "x", "extra"],  # 6: bad_column_count
        list(JOURNAL_HEADER),  # row 7: repeated header
        ["5", "700001", "2026-09-20", "10:00:01", "f"],  # row 8: empty last cell = ""
    ]
    parsed = parse_journal(workbook(rows))
    assert [(item.line_no, item.reason) for item in parsed.quarantined] == [
        (4, "empty_channel"),
        (5, "bad_date"),
        (6, "bad_column_count"),
    ]
    assert [(row.line_no, row.value_raw) for row in parsed.rows] == [(2, "ok"), (8, "")]
    assert parsed.technical_headers == 1


def test_xlsx_reference_is_parsed_like_csv():
    rows = [
        list(CHANNELS_HEADER),
        [56682, "Электроснабжение", "Датчик температуры", "МК-1.1.1.1.1.1", "Т ПК1", 56],
    ]
    parsed = parse_reference(workbook(rows), "channels")
    assert parsed.container == "xlsx"
    assert parsed.rows == [
        (2, "56682", "Электроснабжение", "Датчик температуры", "МК-1.1.1.1.1.1", "Т ПК1", "56")
    ]


def test_bad_workbooks_are_rejected_before_parsing(monkeypatch):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("readme.txt", "not a workbook")
    with pytest.raises(FileRejected) as error:
        parse_journal(archive.getvalue())
    assert error.value.code == "unknown_format"
    with pytest.raises(FileRejected) as error:
        parse_journal(workbook([["ид", "канал"], [1, 2]]))
    assert error.value.code == "bad_header"
    monkeypatch.setattr(xlsx_source, "MAX_XLSX_ROWS", 2)
    with pytest.raises(FileRejected) as error:
        parse_journal(workbook(typed_rows(ROWS)))
    assert error.value.code == "file_too_large"


def tz_csv(rows, *, delimiter: str = ",", header=TZ_JOURNAL_HEADER) -> bytes:
    def cell(text: str) -> str:
        return f'"{text}"' if delimiter in text else text

    lines = [delimiter.join(header)]
    lines += [delimiter.join(cell(value) for value in row) for row in rows]
    return ("\n".join(lines) + "\n").encode()


@pytest.mark.parametrize("delimiter", [",", ";"])
def test_tz_appendix1_example_is_accepted(delimiter):
    parsed = parse_journal(tz_csv(TZ_EXAMPLE, delimiter=delimiter))
    assert parsed.layout == "tz_appendix1"
    assert parsed.quarantined == []
    first = parsed.rows[0]
    assert (first.event_id, first.channel_id, first.channel_type_id) == (
        "3008235019",
        "56682",
        "12",
    )
    assert first.value_raw == "25,00" and first.value_numeric is None
    # Moscow local time, minute precision; the source text is kept.
    assert first.event_at == datetime(2026, 10, 19, 9, 15, tzinfo=UTC)
    assert first.event_local_raw == "19.10.2026 12:15"
    # No alarm column: unknown, never false.
    assert [row.alarm for row in parsed.rows] == [None, None, None]
    assert parsed.alarm_not_provided == 3 and parsed.minute_precision == 3


def test_tz_header_is_compared_loosely_and_xlsx_works():
    header = ["ид_записи_журнала", "ИД  канала данных", "ид типа канала данных", "ТЕКУЩЕЕ ЗНАЧЕНИЕ"]
    header.append("Дата записи ")
    parsed = parse_journal(tz_csv(TZ_EXAMPLE, header=header))
    assert parsed.layout == "tz_appendix1" and len(parsed.rows) == 3
    typed = [list(TZ_JOURNAL_HEADER)]
    for event_id, channel, type_id, value, recorded in TZ_EXAMPLE:
        moment = datetime.strptime(recorded, "%d.%m.%Y %H:%M")
        typed.append([int(event_id), int(channel), int(type_id), value, moment])
    from_xlsx = parse_journal(workbook(typed, formats={(2, 5): "dd.mm.yyyy hh:mm"}))
    assert from_xlsx.layout == "tz_appendix1" and from_xlsx.container == "xlsx"
    assert [dedup_identity(row) for row in from_xlsx.rows] == [
        dedup_identity(row) for row in parsed.rows
    ]


def test_tz_record_time_rules():
    rows = [
        ("1", "56682", "12", "25,00", "2026-10-19 12:15:30"),  # ISO with seconds
        ("2", "56682", "12", "25,00", "19.10.2026"),  # no time: bad_time
        ("3", "56682", "12", "25,00", "32.10.2026 12:15"),  # bad_date
        ("4", "56682", "12", "25,00", "19.10.2026 25:15"),  # bad_time
        ("5", "", "12", "25,00", "19.10.2026 12:15"),  # empty_channel
        ("6", "56682", "", "", "01.01.1970 03:00"),  # placeholder time, empty type
    ]
    parsed = parse_journal(tz_csv(rows))
    assert [(item.line_no, item.reason) for item in parsed.quarantined] == [
        (3, "bad_time"),
        (4, "bad_date"),
        (5, "bad_time"),
        (6, "empty_channel"),
    ]
    first, placeholder = parsed.rows
    assert first.event_at.second == 30
    assert placeholder.is_epoch_placeholder and placeholder.channel_type_id is None
    assert parsed.minute_precision == 1


def test_tz_type_id_is_checked_against_the_reference_type():
    assert not contradicts("12", "Датчик температуры")
    assert contradicts("12", "Состояние фазы")
    assert not contradicts("2", "КД АВ")
    # Unknown ID, unknown reference type: nothing to contradict.
    assert not contradicts("99", "Состояние фазы")
    assert not contradicts("12", None)
    assert not contradicts(None, "Состояние фазы")

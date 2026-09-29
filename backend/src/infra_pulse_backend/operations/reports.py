"""Management reports (R1, ТЗ §8, optional): monthly summary (JSON and XLSX) and the
forecast journal (XLSX). PDF is printed from the browser page; there is no server PDF.

Sources, the same in every mode: journal entries of forecast cards (``forecast_db`` from
B2, ``forecast_fixture`` in fixture mode) and source-alarm counts of the observation
scope (``storage/reports_pg.py``). The XLSX renders the ``MonthlyReport`` object itself,
so JSON and XLSX numbers cannot differ. Counters are registered events of the journal,
not a quality metric; P and R stay on the research side.

- Cards of a month: scored cards issued in it (issue date in MSK). Each card is counted
  once as «снята по событию» (realized), «без события» (not realized), «неизвестно»
  (unknown) or «открыто» (no outcome yet), so they add up to «выдано».
- Decisions: the latest decision revision of each card of the month, codes R1–R7.
- Source alarms: records with the source ``alarm=true`` whose event time lies in the
  month and not later than the data watermark. ``alarm`` is not a confirmed failure.
- XLSX: Russian headers, MSK dates without time zone, values only. Text is written as
  text: a value starting with «=» never becomes a formula; control characters are shown
  as ``\\xNN`` instead of being dropped. The journal is written in write-only mode page
  by page, so memory does not grow with the number of rows; the period is ≤ 93 days.
- Nothing identifies a session or a password; note texts are not read. Free text of a
  person is limited to the decision reason and «кому сообщено»; the decision author is
  shown by role, not by login.
"""

from __future__ import annotations

import re
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import IO, TYPE_CHECKING, Literal

from infra_pulse_backend.ingestion.journal_csv import MSK
from infra_pulse_core.contracts.auth import ROLE_LABELS
from infra_pulse_core.contracts.forecast import (
    HORIZON_HOURS,
    TARGET_LABELS,
    DecisionCode,
    ForecastJournalEntry,
    ForecastJournalList,
    ForecastMode,
    TargetSpecId,
)
from infra_pulse_core.contracts.reports import (
    MAX_TOP_OBJECTS,
    MonthlyReport,
    ReportAlarmTypeRow,
    ReportCardCounts,
    ReportDecisionCount,
    ReportObjectRow,
)

if TYPE_CHECKING:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell

MAX_JOURNAL_DAYS = 93
JOURNAL_PAGE_LIMIT = 100
DECISION_CODES: tuple[DecisionCode, ...] = ("R1", "R2", "R3", "R4", "R5", "R6", "R7")
UNKNOWN_SENSOR_TYPE = "тип не указан"
# Excel allows at most 31 characters in a sheet name (test_reports.py checks it).
ALARMS_SHEET = "Тревожные сообщения по типам"
CardFact = Literal["released", "no_event", "unknown", "open"]
FACT_LABELS: dict[str, str] = {
    "released": "снята по событию",
    "no_event": "без события",
    "unknown": "неизвестно",
    "open": "открыто",
}
MODE_LABELS: dict[str, str] = {
    "fixture": "синтетические данные (fixture)",
    "replay": "воспроизведение истории (replay)",
    "received": "загруженные данные (received)",
    "live": "поток данных (live)",
}
RISK_LABELS = {"high": "высокий", "medium": "средний", "low": "низкий", "unknown": "нет оценки"}
_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_DATE_FORMAT = "DD.MM.YYYY HH:MM"
_ILLEGAL = re.compile(r"[\000-\010\013\014\016-\037]")
BASIS_TEXT = "зарегистрированные события журнала; не метрика качества прогноза"


class ReportRequestError(ValueError):
    """Bad month or period (HTTP 422 with the message as detail)."""


def parse_month(month: str) -> tuple[date, date]:
    """``YYYY-MM`` -> first day of the month and first day of the next month."""
    match = _MONTH.fullmatch(month)
    if match is None:
        raise ReportRequestError("invalid_month")
    year, number = int(match.group(1)), int(match.group(2))
    start = date(year, number, 1)
    end = date(year + number // 12, number % 12 + 1, 1)
    return start, end


def local_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=MSK)


def check_journal_period(issued_from: date, issued_to: date) -> None:
    """Inclusive issue dates, at most ``MAX_JOURNAL_DAYS`` days."""
    if issued_from > issued_to:
        raise ReportRequestError("issued_range_reversed")
    if (issued_to - issued_from).days + 1 > MAX_JOURNAL_DAYS:
        raise ReportRequestError("journal_period_exceeds_93_days")


def iter_journal(
    fetch_page: Callable[[str | None], ForecastJournalList],
) -> Iterator[ForecastJournalEntry]:
    """Every entry of a journal query, following its pinned cursor page by page."""
    cursor: str | None = None
    while True:
        page = fetch_page(cursor)
        yield from page.items
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def issued_local_date(entry: ForecastJournalEntry) -> date:
    return entry.card.issued_at.astimezone(MSK).date()


def card_fact(entry: ForecastJournalEntry) -> CardFact:
    status = entry.outcome.status
    if entry.list_state == "released" or status == "realized":
        return "released"
    if status == "not_realized":
        return "no_event"
    if status == "unknown":
        return "unknown"
    return "open"


@dataclass
class AlarmCounts:
    """Source alarms (``alarm=true``) of the month by sensor type and by object."""

    by_type: dict[str, int] = field(default_factory=dict)
    by_object: dict[str, int] = field(default_factory=dict)


@dataclass
class _ObjectTally:
    cards: int = 0
    released: int = 0


def month_notes(*, mode: ForecastMode, period_end: date, data_as_of: datetime | None) -> list[str]:
    notes = [
        "Счётчики — зарегистрированные события журнала, не метрика качества прогноза.",
        "Карточки — выданные в месяце по дате выдачи (МСК); каждая учтена один раз: "
        "снята по событию, без события, неизвестно или открыто (исход ещё не определён).",
        "Решения — последняя ревизия решения по карточкам месяца (коды R1–R7).",
        "Тревожные сообщения — исходные записи с alarm=true по времени события; флаг alarm "
        "не означает подтверждённый отказ.",
        "Время — МСК (UTC+3).",
    ]
    if data_as_of is None or data_as_of < local_midnight(period_end):
        notes.append(
            "Месяц не завершён на момент данных: открытые карточки и счётчики ещё изменятся."
        )
    if mode == "fixture":
        notes.append("Синтетические данные (fixture), не данные заказчика.")
    return notes


def build_monthly_report(
    *,
    mode: ForecastMode,
    month: str,
    generated_at: datetime,
    data_as_of: datetime | None,
    entries: Iterable[ForecastJournalEntry],
    targets: Iterable[TargetSpecId] = (),
    alarms: AlarmCounts | None = None,
    object_names: dict[str, str] | None = None,
) -> MonthlyReport:
    """Monthly summary from journal entries and source-alarm counts.

    ``targets`` get a zero row even without cards (an empty month is a report of zeros).
    Entries outside the month and abstained cards are ignored.
    """
    start, end = parse_month(month)
    alarms = alarms or AlarmCounts()
    names = object_names or {}
    counts: dict[str, Counter[str]] = {target: Counter() for target in targets}
    decisions: Counter[str] = Counter()
    objects: dict[str, _ObjectTally] = {}
    for entry in entries:
        card = entry.card
        if card.status != "scored" or not start <= issued_local_date(entry) < end:
            continue
        fact = card_fact(entry)
        counts.setdefault(card.target_spec_id, Counter())[fact] += 1
        if entry.decision is not None:
            decisions[entry.decision.decision_code] += 1
        if card.object_id is not None:
            tally = objects.setdefault(card.object_id, _ObjectTally())
            tally.cards += 1
            tally.released += fact == "released"
    for object_id in alarms.by_object:
        objects.setdefault(object_id, _ObjectTally())
    rows = [
        ReportObjectRow(
            object_id=object_id,
            object_name=names.get(object_id),
            cards=tally.cards,
            released=tally.released,
            source_alarms=alarms.by_object.get(object_id, 0),
        )
        for object_id, tally in objects.items()
    ]
    rows.sort(key=lambda row: (-row.cards, -row.released, -row.source_alarms, row.object_id))
    return MonthlyReport(
        mode=mode,
        month=month,
        period_start=start,
        period_end=end,
        generated_at=generated_at,
        data_as_of=data_as_of,
        cards=[
            ReportCardCounts(
                target_spec_id=target,
                issued=sum(tally.values()),
                released=tally["released"],
                no_event=tally["no_event"],
                unknown=tally["unknown"],
                open=tally["open"],
            )
            for target, tally in sorted(counts.items())
        ],
        decisions=[
            ReportDecisionCount(decision_code=code, count=decisions[code])
            for code in DECISION_CODES
        ],
        top_objects=rows[:MAX_TOP_OBJECTS],
        alarms_by_type=[
            ReportAlarmTypeRow(sensor_type=sensor_type, source_alarms=count)
            for sensor_type, count in sorted(
                alarms.by_type.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        notes=month_notes(mode=mode, period_end=end, data_as_of=data_as_of),
    )


# --- XLSX -----------------------------------------------------------------------------


def local_naive(value: datetime | None) -> datetime | None:
    """MSK wall time without tzinfo (Excel has no time zones)."""
    return None if value is None else value.astimezone(MSK).replace(tzinfo=None)


def _safe_text(value: str) -> str:
    return _ILLEGAL.sub(lambda match: f"\\x{ord(match.group()):02x}", value)


def _new_workbook() -> Workbook:
    """Write-only workbook. openpyxl is imported on the first export, not at API start:
    it probes for numpy, which the HTTP boundary tests keep out of fixture routes."""
    from openpyxl import Workbook

    return Workbook(write_only=True)


def _cell(sheet, value, *, bold: bool = False) -> WriteOnlyCell:
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font

    if isinstance(value, datetime):
        cell = WriteOnlyCell(sheet, value=local_naive(value))
        cell.number_format = _DATE_FORMAT
    elif isinstance(value, date):
        cell = WriteOnlyCell(sheet, value=value)
        cell.number_format = "DD.MM.YYYY"
    elif isinstance(value, str):
        cell = WriteOnlyCell(sheet, value=_safe_text(value))
        cell.data_type = "s"  # never a formula, whatever the text starts with
    else:
        cell = WriteOnlyCell(sheet, value=value)
    if bold:
        cell.font = Font(bold=True)
    return cell


def _sheet(book: Workbook, title: str, header: list[str], widths: list[int]):
    sheet = book.create_sheet(title)
    for index, width in enumerate(widths):
        sheet.column_dimensions[chr(ord("A") + index)].width = width
    sheet.freeze_panes = "A2"
    sheet.append([_cell(sheet, name, bold=True) for name in header])
    return sheet


def _append(sheet, values: Iterable) -> None:
    sheet.append([_cell(sheet, value) for value in values])


def _conditions_sheet(book: Workbook, rows: list[tuple[str, object]]) -> None:
    sheet = _sheet(book, "Условия", ["Параметр", "Значение"], [28, 100])
    for name, value in rows:
        _append(sheet, [name, value])


def _save(book: Workbook) -> IO[bytes]:
    """Saved workbook in a spooled temporary file, rewound for streaming."""
    target = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    book.save(target)
    target.seek(0)
    return target


def monthly_report_xlsx(report: MonthlyReport) -> IO[bytes]:
    """Sheets «Карточки», «Решения», «Объекты», «Тревожные сообщения по типам», «Условия»."""
    book = _new_workbook()
    cards = _sheet(
        book,
        "Карточки",
        ["Прогноз", "Цель", "Выдано", "Снята по событию", "Без события", "Неизвестно", "Открыто"],
        [34, 34, 10, 18, 14, 13, 10],
    )
    for row in report.cards:
        _append(
            cards,
            [
                TARGET_LABELS[row.target_spec_id],
                row.target_spec_id,
                row.issued,
                row.released,
                row.no_event,
                row.unknown,
                row.open,
            ],
        )
    decisions = _sheet(book, "Решения", ["Код решения", "Решений"], [14, 10])
    for row in report.decisions:
        _append(decisions, [row.decision_code, row.count])
    objects = _sheet(
        book,
        "Объекты",
        [
            "Место",
            "Объект",
            "Объект (ID)",
            "Карточек",
            "Снято по событию",
            "Исходных тревожных сообщений",
        ],
        [8, 40, 24, 10, 18, 30],
    )
    for place, row in enumerate(report.top_objects, start=1):
        _append(
            objects,
            [
                place,
                row.object_name or "",
                row.object_id,
                row.cards,
                row.released,
                row.source_alarms,
            ],
        )
    alarms = _sheet(book, ALARMS_SHEET, ["Тип датчика", "Исходных тревожных сообщений"], [34, 30])
    for row in report.alarms_by_type:
        _append(alarms, [row.sensor_type, row.source_alarms])
    _conditions_sheet(
        book,
        [
            ("Отчёт", "Месячная сводка для руководства"),
            ("Месяц", report.month),
            ("Период с (МСК)", report.period_start),
            ("Период по (МСК, не включая)", report.period_end),
            ("Режим данных", MODE_LABELS[report.mode]),
            ("Сформирован (МСК)", report.generated_at),
            ("Данные по (МСК)", report.data_as_of or "нет опубликованных данных"),
            ("Основание", BASIS_TEXT),
            *(("Примечание", note) for note in report.notes),
        ],
    )
    return _save(book)


JOURNAL_HEADER = [
    "Позиция журнала",
    "Выдано (МСК)",
    "Опубликовано (МСК)",
    "Карточка",
    "Прогноз",
    "Горизонт, сут",
    "Объект",
    "Объект (ID)",
    "Уровень",
    "Строка события",
    "Факт",
    "Время события (МСК)",
    "Снята из списка (МСК)",
    "Решение",
    "Причина решения",
    "Решение принято (МСК)",
    "Роль автора решения",
    "Кому сообщено",
    "Когда сообщено (МСК)",
    "Решение смоделировано",
]
_JOURNAL_WIDTHS = [10, 17, 17, 28, 30, 9, 34, 22, 11, 60, 18, 17, 17, 9, 50, 17, 16, 40, 17, 12]


def journal_row(entry: ForecastJournalEntry, object_name: str | None = None) -> list:
    card, outcome, decision = entry.card, entry.outcome, entry.decision
    return [
        entry.journal_position,
        card.issued_at,
        card.published_at,
        card.id,
        card.target_label,
        HORIZON_HOURS[card.horizon] // 24,
        object_name or "",
        card.object_id or "",
        RISK_LABELS[card.risk_level],
        card.event_label,
        FACT_LABELS[card_fact(entry)],
        outcome.first_event_at,
        entry.released_at,
        decision.decision_code if decision else "",
        f"{decision.reason_code} {decision.reason_text}" if decision else "",
        decision.decided_at if decision else None,
        ROLE_LABELS.get(decision.actor_role, decision.actor_role) if decision else "",
        (decision.notified_to or "") if decision else "",
        decision.notified_at if decision else None,
        ("да" if decision.simulated else "нет") if decision else "",
    ]


@dataclass(frozen=True)
class JournalExport:
    file: IO[bytes]
    rows: int


def journal_xlsx(
    entries: Iterable[ForecastJournalEntry],
    *,
    mode: ForecastMode,
    issued_from: date,
    issued_to: date,
    generated_at: datetime,
    data_as_of: datetime | None,
    object_names: Callable[[list[str]], dict[str, str]] | None = None,
) -> JournalExport:
    """Scored cards issued in ``[issued_from, issued_to]`` (MSK), newest journal first.

    ``entries`` are consumed once, in pages of ``JOURNAL_PAGE_LIMIT``; object names are
    resolved per page, so neither the entries nor the names are held in memory.
    """
    check_journal_period(issued_from, issued_to)
    book = _new_workbook()
    sheet = _sheet(book, "Журнал", JOURNAL_HEADER, _JOURNAL_WIDTHS)
    rows = 0
    page: list[ForecastJournalEntry] = []

    def flush() -> None:
        nonlocal rows
        wanted = sorted({entry.card.object_id for entry in page if entry.card.object_id})
        names = object_names(wanted) if object_names and wanted else {}
        for entry in page:
            _append(sheet, journal_row(entry, names.get(entry.card.object_id or "")))
        rows += len(page)
        page.clear()

    for entry in entries:
        if entry.card.status != "scored":
            continue
        if not issued_from <= issued_local_date(entry) <= issued_to:
            continue
        page.append(entry)
        if len(page) >= JOURNAL_PAGE_LIMIT:
            flush()
    flush()
    _conditions_sheet(
        book,
        [
            ("Отчёт", "Журнал выданных прогнозов"),
            ("Выдано с (МСК)", issued_from),
            ("Выдано по (МСК, включительно)", issued_to),
            ("Строк", rows),
            ("Режим данных", MODE_LABELS[mode]),
            ("Сформирован (МСК)", generated_at),
            ("Данные по (МСК)", data_as_of or "нет опубликованных данных"),
            ("Основание", BASIS_TEXT),
            (
                "Примечание",
                "Только выданные (оценённые) карточки; «прогноз не выдан» в журнал отчёта "
                "не входит. Новые записи журнала сверху.",
            ),
            ("Примечание", "Факт «открыто» — исход карточки ещё не определён."),
            ("Примечание", "Автор решения указан ролью; причина решения — дословно."),
        ],
    )
    return JournalExport(file=_save(book), rows=rows)


def month_window(month: str) -> tuple[datetime, datetime]:
    """Month bounds as aware MSK midnights ``[start, end)``."""
    start, end = parse_month(month)
    return local_midnight(start), local_midnight(end)


def journal_issue_bounds(start: date, end_exclusive: date) -> tuple[date, date]:
    return start, end_exclusive - timedelta(days=1)


def xlsx_file_name(kind: Literal["monthly", "journal"], *parts: object) -> str:
    return "infrapulse-" + "-".join([kind, *(str(part) for part in parts)]) + ".xlsx"

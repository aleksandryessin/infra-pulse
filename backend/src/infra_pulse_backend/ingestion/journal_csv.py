"""Parser of the event journal: CSV or XLSX, two accepted headers.

**Organizers' export** (``LAYOUT_EXPORT``): ``ид_события, ид_канала_данных, дата, время,
тревожное, значение_датчика``; cells quoted with ``true``/``false`` or unquoted with
``t``/``f``; UTF-8 with or without BOM. Rules follow the curated ETL
(``data-science/scripts/curate_journal.py``, ``journal-curated-v4``) so research and
runtime read the same records:

* ``дата`` ``YYYY-MM-DD`` + ``время`` ``HH:MM:SS[.ffffff]`` is Moscow local time
  with the fixed offset UTC+03:00 (assumed; no timezone in the source). The source
  text ``"<дата> <время>"`` is kept as ``event_local_raw``.
* ``тревожное`` is exactly ``t``/``true``/``f``/``false``; anything else is quarantined.
* ``значение_датчика`` is kept verbatim; ``value_numeric`` is set only for a plain
  decimal with a dot (``1,5`` and state words stay text-only).
* Local time 1970-01-01 03:00:00/01 or the value ``01.01.1970 03:00:00/01`` is a
  device placeholder, flagged ``is_epoch_placeholder`` and never rewritten.
* ``ид_события`` is kept as text and is not unique (DATA-03).

**ТЗ Appendix 1** (``LAYOUT_TZ``, G1 27.09.2026): ``ИД записи журнала, ИД канала данных,
ИД типа канала данных, Текущее значение, Дата записи`` — the header is compared
loosely (case, ``_``/spaces), the CSV separator may be ``,`` or ``;``:

* ``ИД записи журнала`` is the event ID (text), ``ИД канала данных`` the channel,
  ``Текущее значение`` the verbatim value (``25,00`` stays text; ``value_numeric``
  follows the same dot-only rule);
* ``Дата записи`` is one cell ``DD.MM.YYYY HH:MM[:SS[.ffffff]]`` (as in the ТЗ table)
  or ``YYYY-MM-DD HH:MM[:SS[.ffffff]]``, Moscow local time as above; the text is kept
  as ``event_local_raw``; a bad date part is ``bad_date``, a missing or bad time
  ``bad_time``;
* the layout has no alarm column: ``alarm`` is ``None`` — «не передан источником»,
  never ``false`` and never derived from the text (stored as NULL, see ``load_pg``);
* ``ИД типа канала данных`` is kept verbatim as ``channel_type_id``; the sensor type
  comes from the channel reference, a contradicting ID is quarantined at load
  (``channel_types``);
* inside one minute the load order follows «ИД записи журнала» (``load_pg``).

The deduplication key does not depend on the layout or the container, so the same
record loaded from CSV, XLSX or an API batch is stored once.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

from infra_pulse_backend.ingestion.csv_source import (
    Container,
    FileRejected,
    Layout,
    Quarantined,
    check_cells,
    open_records,
)

JOURNAL_HEADER = (
    "ид_события",
    "ид_канала_данных",
    "дата",
    "время",
    "тревожное",
    "значение_датчика",
)
# ТЗ «Прогнозирование отказов …», Приложение 1 «Пример журнала сработок».
TZ_JOURNAL_HEADER = (
    "ИД записи журнала",
    "ИД канала данных",
    "ИД типа канала данных",
    "Текущее значение",
    "Дата записи",
)
LAYOUT_EXPORT = Layout("organizers_export", JOURNAL_HEADER)
LAYOUT_TZ = Layout("tz_appendix1", TZ_JOURNAL_HEADER, loose=True, delimiters=(",", ";"))
JOURNAL_LAYOUTS = (LAYOUT_EXPORT, LAYOUT_TZ)
# Curated ETL: "Europe/Moscow (assumed; UTC=local-03:00 for 2019-2026)".
MSK = timezone(timedelta(hours=3), "MSK")
ALARM_VALUES = {"t": True, "true": True, "f": False, "false": False}
EPOCH_EVENTS = {
    datetime(1970, 1, 1, 3, 0, 0, tzinfo=MSK),
    datetime(1970, 1, 1, 3, 0, 1, tzinfo=MSK),
}
EPOCH_VALUES = {"01.01.1970 03:00:00", "01.01.1970 03:00:01"}
NUMERIC = re.compile(r"[-+]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)\Z")
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})\Z")
_TIME = re.compile(r"(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?\Z")
# ТЗ Appendix 1 «Дата записи»: day first (as printed) or ISO, seconds optional.
_TZ_DAY_FIRST = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\Z")
_TZ_TIME = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?\Z")


@dataclass(slots=True)
class JournalRow:
    line_no: int
    record_ordinal: int
    event_id: str | None
    channel_id: str
    event_at: datetime
    event_local_raw: str
    # None: the source layout has no alarm column (ТЗ Appendix 1), not «false».
    alarm: bool | None
    value_raw: str
    value_numeric: float | None
    is_epoch_placeholder: bool
    # ТЗ Appendix 1 «ИД типа канала данных», verbatim; None in the organizers' export.
    channel_type_id: str | None = None
    # ТЗ Appendix 1 «Дата записи» without seconds.
    minute_only: bool = False


@dataclass(slots=True)
class ParsedJournal:
    rows: list[JournalRow] = field(default_factory=list)
    quarantined: list[Quarantined] = field(default_factory=list)
    technical_headers: int = 0
    # Which header and container were recognised (import report and audit).
    layout: str = LAYOUT_EXPORT.name
    container: Container = "csv"

    @property
    def alarm_not_provided(self) -> int:
        return sum(row.alarm is None for row in self.rows)

    @property
    def minute_precision(self) -> int:
        """ТЗ Appendix 1 records whose «Дата записи» has no seconds."""
        return sum(row.minute_only for row in self.rows)

    @property
    def records(self) -> int:
        return len(self.rows) + len(self.quarantined)

    def period(self) -> tuple[datetime, datetime] | None:
        """Event period of valid records, ignoring the 1970 placeholder timestamps."""
        times = [row.event_at for row in self.rows if row.event_at not in EPOCH_EVENTS]
        return (min(times), max(times)) if times else None


def numeric_value(value_raw: str) -> float | None:
    if not NUMERIC.match(value_raw):
        return None
    number = float(value_raw)
    return number if math.isfinite(number) else None


def _date(text: str, cache: dict[str, tuple[int, int, int] | None]) -> tuple[int, int, int] | None:
    if text in cache:
        return cache[text]
    match = _DATE.match(text)
    parsed = None
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            datetime(year, month, day)
            parsed = (year, month, day)
        except ValueError:
            parsed = None
    cache[text] = parsed
    return parsed


def _event_at(day: tuple[int, int, int], text: str) -> datetime | None:
    match = _TIME.match(text)
    if match is None:
        return None
    hour, minute, second, fraction = match.groups()
    micro = int(fraction.ljust(6, "0")) if fraction else 0
    try:
        return datetime(*day, int(hour), int(minute), int(second), micro, tzinfo=MSK)
    except ValueError:
        return None


def _tz_moment(
    text: str, cache: dict[str, tuple[int, int, int] | None]
) -> tuple[datetime | None, str | None]:
    """ТЗ «Дата записи»: (event time, quarantine reason); minute precision -> ``_MINUTE``."""
    parts = text.strip().replace("T", " ", 1).split()
    if not parts:
        return None, "bad_date"
    day_text = parts[0]
    match = _TZ_DAY_FIRST.match(day_text)
    iso = f"{match[3]}-{match[2]}-{match[1]}" if match else day_text
    day = _date(iso, cache)
    if day is None:
        return None, "bad_date"
    if len(parts) != 2:
        return None, "bad_time"
    clock = _TZ_TIME.match(parts[1])
    if clock is None:
        return None, "bad_time"
    hour, minute, second, fraction = clock.groups()
    micro = int(fraction.ljust(6, "0")) if fraction else 0
    try:
        moment = datetime(*day, int(hour), int(minute), int(second or 0), micro, tzinfo=MSK)
    except ValueError:
        return None, "bad_time"
    return moment, (_MINUTE if second is None else None)


_MINUTE = "minute"  # not a quarantine reason: the time is valid, without seconds


def _parse_tz(parsed: ParsedJournal, records) -> None:
    dates: dict[str, tuple[int, int, int] | None] = {}
    for line_no, ordinal, cells in records:
        if ordinal == 0:
            parsed.technical_headers += 1
            continue
        reason = check_cells(cells, 5)
        if reason is None:
            event_id, channel_id, type_id, value_raw, recorded_raw = cells
            event_at, reason = _tz_moment(recorded_raw, dates)
            minute_only = reason == _MINUTE
            if minute_only:
                reason = None
            if not channel_id:
                reason = "empty_channel"
            elif event_at is not None:
                parsed.rows.append(
                    JournalRow(
                        line_no=line_no,
                        record_ordinal=ordinal,
                        event_id=event_id or None,
                        channel_id=channel_id,
                        event_at=event_at,
                        event_local_raw=recorded_raw,
                        alarm=None,
                        value_raw=value_raw,
                        value_numeric=numeric_value(value_raw),
                        is_epoch_placeholder=event_at in EPOCH_EVENTS or value_raw in EPOCH_VALUES,
                        channel_type_id=type_id or None,
                        minute_only=minute_only,
                    )
                )
                continue
        parsed.quarantined.append(Quarantined(line_no, ordinal, reason, cells))


def parse_journal(data: bytes) -> ParsedJournal:
    """Parse the whole file; raises FileRejected only for header/encoding of the file."""
    layout, container, records = open_records(data, JOURNAL_LAYOUTS)
    parsed = ParsedJournal(layout=layout.name, container=container)
    if layout is LAYOUT_TZ:
        _parse_tz(parsed, records)
        return parsed
    dates: dict[str, tuple[int, int, int] | None] = {}
    rows = parsed.rows
    quarantined = parsed.quarantined
    for line_no, ordinal, cells in records:
        if ordinal == 0:
            parsed.technical_headers += 1
            continue
        reason = check_cells(cells, 6)
        if reason is None:
            event_id, channel_id, date_raw, time_raw, alarm_raw, value_raw = cells
            day = _date(date_raw, dates)
            event_at = _event_at(day, time_raw) if day is not None else None
            alarm = ALARM_VALUES.get(alarm_raw)
            if not channel_id:
                reason = "empty_channel"
            elif day is None:
                reason = "bad_date"
            elif event_at is None:
                reason = "bad_time"
            elif alarm is None:
                reason = "bad_bool"
            else:
                rows.append(
                    JournalRow(
                        line_no=line_no,
                        record_ordinal=ordinal,
                        event_id=event_id or None,
                        channel_id=channel_id,
                        event_at=event_at,
                        event_local_raw=f"{date_raw} {time_raw}",
                        alarm=alarm,
                        value_raw=value_raw,
                        value_numeric=numeric_value(value_raw),
                        is_epoch_placeholder=event_at in EPOCH_EVENTS or value_raw in EPOCH_VALUES,
                    )
                )
                continue
        quarantined.append(Quarantined(line_no, ordinal, reason, cells))
    return parsed


def dedup_identity(row: JournalRow) -> str:
    """Overlap key (channel, time, event ID, value); the event ID alone is not unique."""
    return "\x00".join(
        (
            row.channel_id,
            row.event_at.astimezone(UTC).isoformat(),
            "" if row.event_id is None else "e" + row.event_id,
            row.value_raw,
        )
    )


__all__ = [
    "JOURNAL_HEADER",
    "JOURNAL_LAYOUTS",
    "MSK",
    "TZ_JOURNAL_HEADER",
    "FileRejected",
    "JournalRow",
    "ParsedJournal",
    "dedup_identity",
    "numeric_value",
    "parse_journal",
]

"""Parser of an observation batch posted to ``POST /api/v1/observations`` (B1x).

The stored file is the request body as sent: JSON ``{"batch_id": ..., "records": [...]}``
or the same batch in XML (``ingestion/batch_xml.py``, stored as ``<import>.xml``).
Each record carries the journal fields under English or CSV names:

* ``event_id`` / ``ид_события`` — text or integer, optional, kept as text (not unique);
* ``channel_id`` / ``ид_канала_данных`` — text or integer, required;
* ``date`` + ``time`` (``дата`` + ``время``) — Moscow local time, the CSV rule
  (``journal_csv``: fixed UTC+03:00), **or** ``event_at`` — ISO 8601 with an offset,
  read by the contract's rule (pydantic ``AwareDatetime``), never both;
* ``alarm`` / ``тревожное`` — JSON ``true``/``false``;
* ``value`` / ``значение_датчика`` — a JSON string kept verbatim.

Records become the same ``JournalRow`` as CSV lines, so ``dedup_identity`` and
``row_uid`` are shared: a record already loaded from CSV or an earlier batch is
counted as a duplicate. The record number (1-based position in ``records``) is the
``line_no`` of quarantine rows. A bad record is quarantined with its number; the
others are accepted. The HTTP layer validates the batch against the contract first,
so here quarantine covers what the schema cannot see (calendar dates, clock times,
text that PostgreSQL cannot store, over-long cells).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, TypeAdapter, ValidationError

from infra_pulse_backend.ingestion.batch_xml import BatchXmlError, batch_from_xml
from infra_pulse_backend.ingestion.csv_source import (
    MAX_CELL_CHARS,
    FileRejected,
    Quarantined,
    is_clean,
)
from infra_pulse_backend.ingestion.journal_csv import (
    EPOCH_EVENTS,
    EPOCH_VALUES,
    JournalRow,
    ParsedJournal,
    _date,
    _event_at,
    numeric_value,
)

# English field -> CSV header name accepted as an alias.
FIELDS = {
    "event_id": "ид_события",
    "channel_id": "ид_канала_данных",
    "date": "дата",
    "time": "время",
    "alarm": "тревожное",
    "value": "значение_датчика",
}
EVENT_AT = "event_at"
BatchContainer = Literal["json", "xml"]
# Same parser as ObservationRecord.event_at, so the API and the worker agree.
_AWARE = TypeAdapter(AwareDatetime)


def _field(record: dict, name: str) -> object:
    if name in record:
        return record[name]
    return record.get(FIELDS[name])


def _text(value: object) -> str | None:
    """A JSON string, or an integer ID written as text; ``None`` for anything else."""
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _cells(record: object) -> list[str]:
    """Record as quarantine cells in the CSV column order (for the administrator)."""
    if not isinstance(record, dict):
        return [_escaped(json.dumps(record, ensure_ascii=False))[:MAX_CELL_CHARS]]
    moment = record.get(EVENT_AT)
    if moment is None:
        moment_cells = [_field(record, "date"), _field(record, "time")]
    else:
        moment_cells = [moment]
    values = [
        _field(record, "event_id"),
        _field(record, "channel_id"),
        *moment_cells,
        _field(record, "alarm"),
        _field(record, "value"),
    ]
    cells = []
    for value in values:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        cells.append(_escaped(text)[:MAX_CELL_CHARS])
    return cells


def _escaped(text: str) -> str:
    """Lone surrogates and NUL as backslash escapes, so the cell can be stored and shown."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8").replace("\x00", "\\x00")


def _moment(record: dict, dates: dict) -> tuple[datetime | None, str, str | None]:
    """(event time, source text, quarantine reason)."""
    raw_at = record.get(EVENT_AT)
    date_raw, time_raw = _field(record, "date"), _field(record, "time")
    if raw_at is not None:
        if date_raw is not None or time_raw is not None or isinstance(raw_at, bool):
            return None, "", "bad_time"
        source = raw_at if isinstance(raw_at, str) else json.dumps(raw_at)
        try:
            return _AWARE.validate_python(raw_at), source, None
        except ValidationError:
            return None, source, "bad_time"
    if not isinstance(date_raw, str):
        return None, "", "bad_date"
    day = _date(date_raw, dates)
    if day is None:
        return None, "", "bad_date"
    if not isinstance(time_raw, str):
        return None, "", "bad_time"
    moment = _event_at(day, time_raw)
    if moment is None:
        return None, "", "bad_time"
    return moment, f"{date_raw} {time_raw}", None


def _storable(text: str) -> bool:
    """No NUL and no lone surrogate (``"\\ud800"`` escapes): PostgreSQL text can hold it."""
    return is_clean(text) and not any("\ud800" <= char <= "\udfff" for char in text)


def _strings_ok(record: dict) -> str | None:
    for name in (*FIELDS, *FIELDS.values(), EVENT_AT):
        value = record.get(name)
        if isinstance(value, str):
            if not _storable(value):
                return "bad_encoding"
            if len(value) > MAX_CELL_CHARS:
                return "value_too_long"
    return None


def parse_batch_records(records: list[object]) -> ParsedJournal:
    parsed = ParsedJournal()
    dates: dict[str, tuple[int, int, int] | None] = {}
    for number, record in enumerate(records, start=1):
        reason: str | None
        if not isinstance(record, dict):
            reason = "bad_column_count"
        else:
            reason = _strings_ok(record)
        if reason is None:
            event_raw = _field(record, "event_id")
            event_id = _text(event_raw) if event_raw is not None else None
            channel_id = _text(_field(record, "channel_id"))
            alarm = _field(record, "alarm")
            value_raw = _field(record, "value")
            event_at, local_raw, reason = _moment(record, dates)
            if event_raw is not None and event_id is None:
                reason = "bad_column_count"
            elif not channel_id:
                reason = "empty_channel"
            elif reason is not None:
                pass
            elif not isinstance(alarm, bool):
                reason = "bad_bool"
            elif not isinstance(value_raw, str):
                reason = "bad_column_count"
            else:
                assert event_at is not None
                parsed.rows.append(
                    JournalRow(
                        line_no=number,
                        record_ordinal=number,
                        event_id=event_id or None,
                        channel_id=channel_id,
                        event_at=event_at,
                        event_local_raw=local_raw,
                        alarm=alarm,
                        value_raw=value_raw,
                        value_numeric=numeric_value(value_raw),
                        is_epoch_placeholder=event_at in EPOCH_EVENTS or value_raw in EPOCH_VALUES,
                    )
                )
                continue
        parsed.quarantined.append(Quarantined(number, number, reason, _cells(record)))
    return parsed


def parse_batch(data: bytes, container: BatchContainer = "json") -> ParsedJournal:
    """Parse the stored request body; raises FileRejected when it is not a batch.

    ``container`` is the body format the API accepted (``xml`` for ``<import>.xml``).
    An XML record arrives as text fields and a boolean alarm, i.e. the JSON shape.
    """
    if container == "xml":
        # The same converter as the API, so the worker never rejects an accepted body.
        try:
            body = batch_from_xml(data)
        except BatchXmlError as error:
            raise FileRejected("bad_header", "batch is not an XML batch") from error
    else:
        # Bytes as Starlette reads the request body (json.loads on bytes: a UTF-8 BOM
        # is accepted), so the worker never rejects a body the API has accepted.
        try:
            body = json.loads(data)
        except UnicodeDecodeError as error:
            raise FileRejected("bad_encoding", "batch is not UTF-8") from error
        except ValueError as error:
            raise FileRejected("bad_header", "batch is not JSON") from error
    records = body.get("records") if isinstance(body, dict) else None
    if not isinstance(records, list):
        raise FileRejected("bad_header", "batch has no records list")
    parsed = parse_batch_records(records)
    parsed.container = container
    return parsed


__all__ = ["FIELDS", "parse_batch", "parse_batch_records"]

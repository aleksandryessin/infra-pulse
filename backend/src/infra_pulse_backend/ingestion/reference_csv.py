"""Parsers of the three reference files (CSV or XLSX): channels, objects and states.

Cells are kept verbatim (IDs as text). An exact repeated row counts as a
duplicate; a channel listed twice with different content is kept as two rows and
treated as ambiguous by the loader (no silent choice between payloads).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from infra_pulse_backend.ingestion.csv_source import (
    Container,
    FileRejected,
    Layout,
    Quarantined,
    check_cells,
    open_records,
)

ReferenceKind = Literal["channels", "objects", "states"]

CHANNELS_HEADER = (
    "ид_канала_данных",
    "тип_инж_системы",
    "тип_датчика",
    "тег_инженерной_системы",
    "название_датчика",
    "ид_объект",
)
OBJECTS_HEADER = (
    "ид_объект",
    "иерархия_уровень",
    "родитель",
    "вид_объекта",
    "диспетчерское_название_объекта",
)
STATES_HEADER = ("тип_датчика", "ид_набор_состояний", "название_состояния", "тревожное")
HEADERS: dict[ReferenceKind, tuple[str, ...]] = {
    "channels": CHANNELS_HEADER,
    "objects": OBJECTS_HEADER,
    "states": STATES_HEADER,
}
FORMAT_KIND: dict[str, ReferenceKind] = {
    "reference_channels_csv": "channels",
    "reference_objects_csv": "objects",
    "reference_states_csv": "states",
}
_BOOL = {"t": True, "true": True, "f": False, "false": False}


@dataclass(slots=True)
class ParsedReference:
    kind: ReferenceKind
    # (line_no, *typed cells) in file order, exact repeats removed.
    rows: list[tuple] = field(default_factory=list)
    duplicates: int = 0
    quarantined: list[Quarantined] = field(default_factory=list)
    technical_headers: int = 0
    container: Container = "csv"

    @property
    def records(self) -> int:
        return len(self.rows) + self.duplicates + len(self.quarantined)


def parse_reference(data: bytes, kind: ReferenceKind) -> ParsedReference:
    header = HEADERS[kind]
    _layout, container, records = open_records(data, (Layout(kind, header),))
    parsed = ParsedReference(kind=kind, container=container)
    seen: set[tuple[str, ...]] = set()
    for line_no, ordinal, cells in records:
        if ordinal == 0:
            parsed.technical_headers += 1
            continue
        reason = check_cells(cells, len(header))
        typed: tuple | None = None
        if reason is None:
            if kind == "channels":
                if not cells[0]:
                    reason = "empty_channel"
                else:
                    typed = (line_no, *cells[:5], cells[5] or None)
            elif kind == "objects":
                typed = (line_no, *cells)
            else:
                alarm = _BOOL.get(cells[3])
                if alarm is None:
                    reason = "bad_bool"
                else:
                    typed = (line_no, cells[0], cells[1], cells[2], alarm)
        if typed is None:
            assert reason is not None
            parsed.quarantined.append(Quarantined(line_no, ordinal, reason, cells))
            continue
        key = tuple(cells)
        if key in seen:
            parsed.duplicates += 1
            continue
        seen.add(key)
        parsed.rows.append(typed)
    return parsed


__all__ = [
    "CHANNELS_HEADER",
    "FORMAT_KIND",
    "HEADERS",
    "OBJECTS_HEADER",
    "STATES_HEADER",
    "FileRejected",
    "ParsedReference",
    "ReferenceKind",
    "parse_reference",
]

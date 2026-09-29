"""Decoding and record iteration shared by the journal and reference parsers.

A file is CSV or XLSX (G1, 27.09.2026); the container is recognised by content, not
by the declared format or the file name: an XLSX workbook is a ZIP archive
(``PK\\x03\\x04``), anything else is read as CSV. Both give the same rows of text
cells, so parsing, quarantine, deduplication and the import report do not depend on
the container (``xlsx_source`` explains how XLSX cells become text).

A CSV file is UTF-8, optionally with a BOM. Undecodable bytes do not fail the whole
file: the affected record is quarantined as ``bad_encoding`` with the bytes
escaped. Records are produced by the CSV parser, not by physical lines, because
quoted cells may contain line breaks; ``line_no`` is the physical line where the
record starts (the header is line 1). For XLSX ``line_no`` is the sheet row number.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Literal

from infra_pulse_core.contracts.imports import MAX_IMPORT_BYTES, QuarantineReason

# Longer cells are quarantined as value_too_long instead of being truncated.
MAX_CELL_CHARS = 4096
BOM = "﻿"
XLSX_MAGIC = b"PK\x03\x04"

HeaderErrorCode = Literal[
    "file_too_large", "unknown_format", "bad_header", "bad_encoding", "no_valid_rows"
]
# Files are CSV or XLSX; API batches (journal_json) are JSON or XML.
Container = Literal["csv", "xlsx", "json", "xml"]


class FileRejected(ValueError):
    """The file cannot be processed at all; maps to ImportFile.error_code."""

    def __init__(self, code: HeaderErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(slots=True)
class Quarantined:
    line_no: int
    record_ordinal: int
    reason: QuarantineReason
    cells: list[str]

    def raw_json(self) -> str:
        return json.dumps([printable(cell) for cell in self.cells], ensure_ascii=False)

    def excerpt(self, limit: int = 200) -> str:
        text = ",".join(printable(cell) for cell in self.cells)
        return text if len(text) <= limit else text[: limit - 1] + "…"


def printable(cell: str) -> str:
    """Escape undecodable bytes and NUL so the text can be stored and shown."""
    if is_clean(cell):
        return cell
    raw = cell.encode("utf-8", "surrogateescape")
    return raw.decode("utf-8", "backslashreplace").replace("\x00", "\\x00")


def is_clean(cell: str) -> bool:
    """False for NUL (not storable in PostgreSQL text) or undecodable bytes."""
    if "\x00" in cell:
        return False
    if cell.isascii():
        return True
    return not any("\udc80" <= char <= "\udcff" for char in cell)


def decode(data: bytes) -> str:
    text = data.decode("utf-8", "surrogateescape")
    return text[1:] if text.startswith(BOM) else text


def is_xlsx(data: bytes) -> bool:
    return data[:4] == XLSX_MAGIC


@dataclass(frozen=True, slots=True)
class Layout:
    """One accepted header of a file kind.

    ``loose``: header cells are compared after case folding, ``ё`` → ``е``, ``_`` → space
    and whitespace collapsing (for headers written by people, as in ТЗ Appendix 1);
    otherwise the header must match exactly. ``delimiters``: CSV separators tried in
    order (``;`` is the Russian-locale spreadsheet export).
    """

    name: str
    header: tuple[str, ...]
    loose: bool = False
    delimiters: tuple[str, ...] = (",",)


_SPACES = re.compile(r"\s+")


def _loose(cell: str) -> str:
    return _SPACES.sub(" ", cell.replace("_", " ").replace("ё", "е").casefold()).strip()


def _matches(cells: Sequence[str], layout: Layout) -> bool:
    if tuple(cells) == layout.header:
        return True
    if not layout.loose:
        return False
    # Trailing empty header cells (a spreadsheet's formatted but unused columns).
    trimmed = list(cells)
    while trimmed and not trimmed[-1].strip():
        trimmed.pop()
    return len(trimmed) == len(layout.header) and all(
        _loose(cell) == _loose(name) for cell, name in zip(trimmed, layout.header, strict=True)
    )


def _csv_rows(text: str, delimiter: str) -> Iterator[tuple[int, list[str]]]:
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=False)
    while True:
        line_no = reader.line_num + 1
        cells = next(reader, None)
        if cells is None:
            return
        yield line_no, cells


def _open_csv(data: bytes, layouts: Sequence[Layout]) -> tuple[Layout, Iterator]:
    csv.field_size_limit(MAX_IMPORT_BYTES)
    text = decode(data)
    delimiters = dict.fromkeys(d for layout in layouts for d in layout.delimiters)
    for delimiter in delimiters:
        rows = _csv_rows(text, delimiter)
        first = next(rows, None)
        if first is None:
            raise FileRejected("bad_header", "empty file")
        cells = first[1]
        if not all(is_clean(cell) for cell in cells):
            raise FileRejected("bad_encoding", "header is not UTF-8")
        for layout in layouts:
            if delimiter in layout.delimiters and _matches(cells, layout):
                return layout, rows
    raise FileRejected("bad_header", "header does not match the declared format")


def _open_xlsx(data: bytes, layouts: Sequence[Layout]) -> tuple[Layout, Iterator]:
    from infra_pulse_backend.ingestion.xlsx_source import xlsx_rows

    # Blank sheet rows are not records (a CSV blank line is not one either).
    rows = ((line_no, cells) for line_no, cells in xlsx_rows(data) if any(cells))
    for _line_no, cells in rows:
        # A sheet is as wide as its widest row: empty cells right of the header go.
        while cells and cells[-1] == "":
            cells = cells[:-1]
        for layout in layouts:
            if _matches(cells, layout):
                width = len(layout.header)
                return layout, ((line_no, _fit(row, width)) for line_no, row in rows)
        raise FileRejected("bad_header", "header does not match the declared format")
    raise FileRejected("bad_header", "empty workbook")


def _fit(cells: list[str], width: int) -> list[str]:
    """XLSX omits empty trailing cells: pad to the header width and drop empty extras.

    A non-empty cell beyond the header stays, so the row is ``bad_column_count``
    exactly as a CSV line with an extra value would be.
    """
    if len(cells) < width:
        return [*cells, *([""] * (width - len(cells)))]
    while len(cells) > width and cells[-1] == "":
        cells = cells[:-1]
    return cells


def open_records(
    data: bytes, layouts: Sequence[Layout]
) -> tuple[Layout, Container, Iterator[tuple[int, int, list[str]]]]:
    """Recognise the container and the header; return the layout and its records.

    Records are ``(line_no, record_ordinal, cells)``. Blank lines (rows) are not
    records. A repeated header line is yielded with ordinal 0 so callers can count
    it separately (as the curated ETL does).
    """
    if len(data) > MAX_IMPORT_BYTES:
        raise FileRejected("file_too_large", "file exceeds the upload limit")
    container: Container = "xlsx" if is_xlsx(data) else "csv"
    opener = _open_xlsx if container == "xlsx" else _open_csv
    layout, rows = opener(data, layouts)
    return layout, container, _numbered(rows, layout)


def _numbered(
    rows: Iterator[tuple[int, list[str]]], layout: Layout
) -> Iterator[tuple[int, int, list[str]]]:
    ordinal = 0
    for line_no, cells in rows:
        if not cells:
            continue
        if _matches(cells, layout):
            yield line_no, 0, cells
            continue
        ordinal += 1
        yield line_no, ordinal, cells


def records(data: bytes, header: tuple[str, ...]) -> Iterator[tuple[int, int, list[str]]]:
    """Records of a file with one exact header (the reference files)."""
    return open_records(data, (Layout("exact", header),))[2]


def check_cells(cells: list[str], width: int) -> QuarantineReason | None:
    """Shape, encoding and length checks common to every format."""
    if len(cells) != width:
        return "bad_column_count"
    for cell in cells:
        if not is_clean(cell):
            return "bad_encoding"
    for cell in cells:
        if len(cell) > MAX_CELL_CHARS:
            return "value_too_long"
    return None

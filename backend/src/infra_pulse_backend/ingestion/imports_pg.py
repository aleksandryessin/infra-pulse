"""Upload storage and the ``import_files`` repository used by HTTP and the worker.

HTTP stores the bytes in the shared upload directory, inserts ``import_files``
(``queued``), a ``jobs`` row and the ``import.uploaded`` audit row (SEC-03, B3
``audit_events``) in one transaction and notifies the worker. It never parses the file.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from infra_pulse_backend.storage.audit_pg import AuditEvent, record_audit_event
from infra_pulse_core.contracts.imports import (
    MAX_QUARANTINE_SAMPLE,
    ImportFile,
    ImportFormat,
    ImportList,
    ImportStageTiming,
    QuarantineSample,
)

NOTIFY_CHANNEL = "infra_import_jobs"
_CHUNK = 1024 * 1024

IMPORT_COLUMNS = """import_id, format, file_name, sha256, size_bytes, uploaded_by,
    uploaded_at, status, finished_at, rows_total, rows_accepted, rows_duplicate,
    rows_quarantined, unknown_channels, event_from, event_to, reference_version,
    duplicate_of, forecast_generation, error_code, timings, quarantine_reasons,
    new_card_ids, released_card_ids, source_layout, source_container,
    alarm_not_provided, notes"""


class UploadTooLarge(ValueError):
    """The upload exceeds the contract limit; nothing is kept."""


def new_import_id() -> str:
    return f"imp-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid4().hex[:12]}"


def stored_path(upload_dir: Path, stored_name: str) -> Path:
    return upload_dir / stored_name


def store_upload(
    source: BinaryIO, upload_dir: Path, stored_name: str, max_bytes: int
) -> tuple[str, int]:
    """Stream the upload to ``upload_dir/stored_name``; return (sha256, size).

    Written to a temporary name, fsynced and renamed, so the worker never sees a
    partial file. Over ``max_bytes`` the temporary file is removed.
    """
    upload_dir.mkdir(parents=True, exist_ok=True)
    partial = upload_dir / f".incoming-{uuid4().hex}.part"
    digest = hashlib.sha256()
    size = 0
    try:
        with partial.open("xb") as target:
            while chunk := source.read(_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise UploadTooLarge("file_too_large")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        os.replace(partial, stored_path(upload_dir, stored_name))
    finally:
        partial.unlink(missing_ok=True)
    return digest.hexdigest(), size


def insert_import(
    connection: psycopg.Connection,
    *,
    import_id: str,
    format: str,
    file_name: str,
    stored_name: str,
    sha256: str,
    size_bytes: int,
    uploaded_by: str,
    store_seconds: float,
) -> dict:
    """Insert the queued ``import_files`` row and its job in the caller's transaction.

    Shared by the CSV upload and the observation API (B1x, ``journal_json``); the
    caller adds its audit row and ``NOTIFY`` to the same transaction.
    """
    timings = [{"stage": "store", "seconds": round(max(store_seconds, 0.0), 4)}]
    row = (
        connection.cursor(row_factory=dict_row)
        .execute(
            f"""INSERT INTO import_files
                (import_id, format, file_name, sha256, size_bytes, stored_name,
                 uploaded_by, status, timings)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'queued', %s)
                RETURNING {IMPORT_COLUMNS}""",
            (
                import_id,
                format,
                file_name,
                sha256,
                size_bytes,
                stored_name,
                uploaded_by,
                Jsonb(timings),
            ),
        )
        .fetchone()
    )
    connection.execute(
        "INSERT INTO jobs (kind, import_id, state) VALUES ('import', %s, 'queued')",
        (import_id,),
    )
    assert row is not None
    return row


def create_import(
    dsn: str,
    *,
    import_id: str,
    format: ImportFormat,
    file_name: str,
    stored_name: str,
    sha256: str,
    size_bytes: int,
    uploaded_by: str,
    store_seconds: float,
    audit: AuditEvent,
) -> ImportFile:
    """Insert the queued import, its job and its audit row atomically, then wake the worker.

    ``audit`` is the ``import.uploaded`` event built by the router from the session
    (actor, acting role, request id, client address); it commits or rolls back together
    with ``import_files`` and ``jobs``.
    """
    if audit.action != "import.uploaded" or audit.target_id != import_id:
        raise ValueError("the upload audit event must target this import")
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        row = insert_import(
            connection,
            import_id=import_id,
            format=format,
            file_name=file_name,
            stored_name=stored_name,
            sha256=sha256,
            size_bytes=size_bytes,
            uploaded_by=uploaded_by,
            store_seconds=store_seconds,
        )
        record_audit_event(connection, audit)
        connection.execute(f"NOTIFY {NOTIFY_CHANNEL}")
    return to_model(row)


def to_model(row: dict, sample: list[dict] | None = None) -> ImportFile:
    return ImportFile(
        id=row["import_id"],
        format=row["format"],
        file_name=row["file_name"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        uploaded_by=row["uploaded_by"],
        uploaded_at=row["uploaded_at"],
        status=row["status"],
        finished_at=row["finished_at"],
        rows_total=row["rows_total"],
        rows_accepted=row["rows_accepted"],
        rows_duplicate=row["rows_duplicate"],
        rows_quarantined=row["rows_quarantined"],
        unknown_channels=row["unknown_channels"],
        event_from=row["event_from"],
        event_to=row["event_to"],
        reference_version=row["reference_version"],
        duplicate_of=row["duplicate_of"],
        forecast_generation=row["forecast_generation"],
        error_code=row["error_code"],
        timings=[ImportStageTiming.model_validate(item) for item in row["timings"]],
        quarantine_reasons=row["quarantine_reasons"],
        quarantine_sample=[QuarantineSample.model_validate(item) for item in sample or []],
        new_card_ids=row["new_card_ids"],
        released_card_ids=row["released_card_ids"],
        source_layout=row["source_layout"],
        source_container=row["source_container"],
        alarm_not_provided=row["alarm_not_provided"],
        notes=row["notes"],
    )


def quarantine_sample(connection: psycopg.Connection, import_id: str) -> list[dict]:
    rows = connection.execute(
        """SELECT line_no, reason, raw_cells FROM import_quarantine
           WHERE import_id = %s ORDER BY line_no LIMIT %s""",
        (import_id, MAX_QUARANTINE_SAMPLE),
    ).fetchall()
    sample = []
    for row in rows:
        text = ",".join(row["raw_cells"])
        sample.append(
            {
                "line_no": row["line_no"],
                "reason": row["reason"],
                "raw_excerpt": text if len(text) <= 200 else text[:199] + "…",
            }
        )
    return sample


def read_import(dsn: str, import_id: str) -> ImportFile | None:
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        row = connection.execute(
            f"SELECT {IMPORT_COLUMNS} FROM import_files WHERE import_id = %s", (import_id,)
        ).fetchone()
        if row is None:
            return None
        return to_model(row, quarantine_sample(connection, import_id))


def list_imports(dsn: str, *, cursor: str | None, limit: int) -> ImportList:
    """Newest first. The cursor is the upload sequence of the last item shown."""
    before = None
    if cursor is not None:
        if not cursor.isdigit() or len(cursor) > 18:
            raise ValueError("invalid_import_cursor")
        before = int(cursor)
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        total = connection.execute("SELECT count(*) AS n FROM import_files").fetchone()["n"]
        rows = connection.execute(
            f"""SELECT seq, {IMPORT_COLUMNS} FROM import_files
                WHERE %s::bigint IS NULL OR seq < %s::bigint
                ORDER BY seq DESC LIMIT %s""",
            (before, before, limit + 1),
        ).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return ImportList(
        items=[to_model(row) for row in rows],
        total=total,
        limit=limit,
        next_cursor=str(rows[-1]["seq"]) if more and rows else None,
    )

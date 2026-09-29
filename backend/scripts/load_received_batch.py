"""Import one bounded local JSON batch and record when this app received it.

This candidate format is for local integration only. It does not connect to a
customer system or claim the source's network delivery timestamp.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from stat import S_ISREG
from typing import Literal

import psycopg
from psycopg.types.json import Jsonb

from infra_pulse_core.contracts.attention import ReceivedBatch

MAX_INPUT_BYTES = 5 * 1024 * 1024
MIGRATION_DIR = Path(__file__).resolve().parents[1] / "migrations"


def read_batch(path: Path) -> tuple[ReceivedBatch, dict, str]:
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("received batch exceeds 5 MiB")
    source_bytes = path.read_bytes()
    if len(source_bytes) > MAX_INPUT_BYTES:
        raise ValueError("received batch exceeds 5 MiB")
    raw = json.loads(source_bytes)
    if not isinstance(raw, dict):
        raise ValueError("received batch must be a JSON object")
    batch = ReceivedBatch.model_validate(raw)
    if any(not isinstance(item["event_at"], str) for item in raw["records"]):
        raise ValueError("event_at must be an ISO timestamp string")
    return batch, raw, hashlib.sha256(source_bytes).hexdigest()


def row_uid(namespace_id: str, stream_id: str, batch_id: str, source_record_id: str) -> str:
    identity = "\x00".join((namespace_id, stream_id, batch_id, source_record_id))
    return hashlib.sha256(identity.encode()).hexdigest()


class BatchConflict(ValueError):
    """A previously accepted batch ID was reused with different bytes."""


def validate_scope(namespace_id: str, stream_id: str) -> None:
    if not namespace_id or not stream_id or len(namespace_id) > 128 or len(stream_id) > 128:
        raise ValueError("namespace and stream must be nonempty and at most 128 characters")


def apply_migrations(connection: psycopg.Connection) -> None:
    for migration in sorted(MIGRATION_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        connection.execute(migration.read_text(encoding="utf-8"))


def ensure_scope(
    connection: psycopg.Connection,
    *,
    namespace_id: str,
    stream_id: str,
    at: datetime,
) -> int:
    connection.execute(
        """INSERT INTO dispatch_replay_snapshots
           (namespace_id, snapshot_id, manifest_sha256, window_start,
            window_end, row_count, alarm_count, scope_kind)
           VALUES (%s, %s, NULL, %s, %s, 0, 0, 'received')
           ON CONFLICT (namespace_id, snapshot_id) DO NOTHING""",
        (
            namespace_id,
            stream_id,
            at - timedelta(microseconds=1),
            at + timedelta(microseconds=1),
        ),
    )
    scope = connection.execute(
        """SELECT scope_kind, row_count FROM dispatch_replay_snapshots
           WHERE namespace_id = %s AND snapshot_id = %s FOR UPDATE""",
        (namespace_id, stream_id),
    ).fetchone()
    if scope is None or scope[0] != "received":
        raise ValueError("namespace and stream collide with a non-received scope")
    return scope[1]


def initialize_stream(dsn: str, *, stream_id: str, namespace_id: str) -> None:
    """Create an empty local stream so validation failures are visible to the API."""
    validate_scope(namespace_id, stream_id)
    with psycopg.connect(dsn) as connection:
        apply_migrations(connection)
        at = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        ensure_scope(connection, namespace_id=namespace_id, stream_id=stream_id, at=at)


def record_inbox_failure(
    dsn: str,
    *,
    namespace_id: str,
    stream_id: str,
    source_name: str,
    error_kind: Literal["invalid_batch", "batch_conflict", "file_unreadable"],
) -> None:
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """INSERT INTO dispatch_received_inbox_failures
               (namespace_id, snapshot_id, source_name, error_kind)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (namespace_id, snapshot_id, source_name)
               DO UPDATE SET error_kind = EXCLUDED.error_kind,
                             detected_at = clock_timestamp()""",
            (namespace_id, stream_id, source_name, error_kind),
        )


def clear_inbox_failure(
    connection: psycopg.Connection,
    *,
    namespace_id: str,
    stream_id: str,
    source_name: str,
) -> None:
    connection.execute(
        """DELETE FROM dispatch_received_inbox_failures
           WHERE namespace_id = %s AND snapshot_id = %s AND source_name = %s""",
        (namespace_id, stream_id, source_name),
    )


def load(
    path: Path,
    *,
    dsn: str,
    stream_id: str,
    namespace_id: str = "local-received",
) -> dict:
    validate_scope(namespace_id, stream_id)
    batch, raw, source_sha256 = read_batch(path)
    source_name = path.name
    with psycopg.connect(dsn) as connection:
        apply_migrations(connection)
        scope_at = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        prior_row_count = ensure_scope(
            connection,
            namespace_id=namespace_id,
            stream_id=stream_id,
            at=scope_at,
        )
        existing = connection.execute(
            """SELECT source_sha256, received_at, row_count, alarm_count
               FROM dispatch_received_batches
               WHERE namespace_id = %s AND snapshot_id = %s AND batch_id = %s""",
            (namespace_id, stream_id, batch.batch_id),
        ).fetchone()
        if existing is not None:
            if existing[0] != source_sha256:
                raise BatchConflict("batch_id was already imported with different content")
            clear_inbox_failure(
                connection,
                namespace_id=namespace_id,
                stream_id=stream_id,
                source_name=source_name,
            )
            return {
                "stream_id": stream_id,
                "batch_id": batch.batch_id,
                "source_sha256": source_sha256,
                "received_at": existing[1].isoformat(),
                "rows": existing[2],
                "alarms": existing[3],
                "repeated": True,
                "availability_basis": "observed_application_import_time",
            }
        # The scope row is locked above. Timestamp the accepted batch after
        # obtaining that lock so competing imports cannot receive timestamps
        # in the opposite order from their committed positions.
        received_at = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        alarm_count = sum(record.alarm for record in batch.records)
        connection.execute(
            """INSERT INTO dispatch_received_batches
               (namespace_id, snapshot_id, batch_id, source_sha256,
                source_name, received_at, row_count, alarm_count)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                namespace_id,
                stream_id,
                batch.batch_id,
                source_sha256,
                source_name,
                received_at,
                len(batch.records),
                alarm_count,
            ),
        )
        rows = []
        for ordinal, record in enumerate(batch.records, start=1):
            flags = []
            if record.object_id is not None:
                flags.append("object_mapping_from_input_unverified")
            if record.event_at > received_at:
                flags.append("source_clock_ahead_of_import")
            rows.append(
                (
                    namespace_id,
                    stream_id,
                    row_uid(namespace_id, stream_id, batch.batch_id, record.source_record_id),
                    record.source_event_id,
                    record.channel_id,
                    record.object_id,
                    record.sensor_type,
                    record.system_type,
                    record.value_raw,
                    record.value_numeric,
                    record.alarm,
                    record.event_at,
                    received_at,
                    "observed",
                    source_name,
                    source_sha256,
                    ordinal,
                    raw["records"][ordinal - 1]["event_at"],
                    record.reference_version,
                    Jsonb(flags),
                    prior_row_count + ordinal,
                )
            )
        with connection.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO dispatch_observations
                   (namespace_id, snapshot_id, row_uid, source_event_id,
                    channel_id, object_id, sensor_type, system_type,
                    value_raw, value_numeric, alarm, event_at, available_at,
                    availability_basis, source_file, source_sha256,
                    record_ordinal, event_local_raw, reference_version, quality_flags,
                    received_position)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                rows,
            )
        connection.execute(
            """UPDATE dispatch_replay_snapshots
               SET row_count = row_count + %s,
                   alarm_count = alarm_count + %s,
                   last_received_at = %s,
                   window_end = GREATEST(window_end, %s)
               WHERE namespace_id = %s AND snapshot_id = %s""",
            (
                len(batch.records),
                alarm_count,
                received_at,
                received_at + timedelta(microseconds=1),
                namespace_id,
                stream_id,
            ),
        )
        clear_inbox_failure(
            connection,
            namespace_id=namespace_id,
            stream_id=stream_id,
            source_name=source_name,
        )
    return {
        "stream_id": stream_id,
        "batch_id": batch.batch_id,
        "source_sha256": source_sha256,
        "received_at": received_at.isoformat(),
        "rows": len(batch.records),
        "alarms": alarm_count,
        "repeated": False,
        "availability_basis": "observed_application_import_time",
    }


def scan_directory(
    directory: Path,
    *,
    dsn: str,
    stream_id: str,
    namespace_id: str,
    seen: dict[str, tuple[int, int, int]],
    max_files: int = 100,
) -> list[dict]:
    """Scan a local inbox once; unchanged rejected files remain visible in the DB."""
    if not directory.is_dir():
        raise NotADirectoryError("received inbox directory is unavailable")
    if max_files < 1:
        raise ValueError("max_files must be positive")
    pending = []
    present = set()
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink():
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if not S_ISREG(stat.st_mode):
            continue
        present.add(path.name)
        signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if seen.get(path.name) == signature:
            continue
        if len(pending) < max_files:
            pending.append((path, signature))

    outcomes = []
    for path, signature in pending:
        try:
            report = load(path, dsn=dsn, stream_id=stream_id, namespace_id=namespace_id)
            outcome = {
                "source_name": path.name,
                "status": "repeated" if report["repeated"] else "imported",
                "rows": report["rows"],
                "received_at": report["received_at"],
            }
        except BatchConflict:
            record_inbox_failure(
                dsn,
                namespace_id=namespace_id,
                stream_id=stream_id,
                source_name=path.name,
                error_kind="batch_conflict",
            )
            outcome = {"source_name": path.name, "status": "batch_conflict"}
        except ValueError:
            record_inbox_failure(
                dsn,
                namespace_id=namespace_id,
                stream_id=stream_id,
                source_name=path.name,
                error_kind="invalid_batch",
            )
            outcome = {"source_name": path.name, "status": "invalid_batch"}
        except FileNotFoundError:
            continue
        except OSError:
            record_inbox_failure(
                dsn,
                namespace_id=namespace_id,
                stream_id=stream_id,
                source_name=path.name,
                error_kind="file_unreadable",
            )
            outcome = {"source_name": path.name, "status": "file_unreadable"}
        if outcome["status"] != "file_unreadable":
            seen[path.name] = signature
        outcomes.append(outcome)

    with psycopg.connect(dsn) as connection:
        failures = connection.execute(
            """SELECT source_name FROM dispatch_received_inbox_failures
               WHERE namespace_id = %s AND snapshot_id = %s""",
            (namespace_id, stream_id),
        ).fetchall()
        removed = [row[0] for row in failures if row[0] not in present]
        if removed:
            connection.execute(
                """DELETE FROM dispatch_received_inbox_failures
                   WHERE namespace_id = %s AND snapshot_id = %s
                     AND source_name = ANY(%s)""",
                (namespace_id, stream_id, removed),
            )
        connection.execute(
            """UPDATE dispatch_replay_snapshots
               SET last_scanned_at = clock_timestamp()
               WHERE namespace_id = %s AND snapshot_id = %s AND scope_kind = 'received'""",
            (namespace_id, stream_id),
        )
    return outcomes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path)
    source.add_argument("--directory", type=Path)
    parser.add_argument("--stream", required=True)
    parser.add_argument("--namespace", default="local-received")
    parser.add_argument("--watch", action="store_true", help="poll a local directory until stopped")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--max-files-per-scan", type=int, default=100)
    args = parser.parse_args()
    dsn = os.environ.get("INFRA_RECEIVED_DSN")
    if not dsn:
        parser.error("INFRA_RECEIVED_DSN is required")
    if args.input is not None:
        if args.watch:
            parser.error("--watch requires --directory")
        report = load(args.input, dsn=dsn, stream_id=args.stream, namespace_id=args.namespace)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return
    if not 0.2 <= args.poll_seconds <= 3600 or not 1 <= args.max_files_per_scan <= 1000:
        parser.error("poll seconds must be 0.2–3600 and max files per scan must be 1–1000")
    if not args.directory.is_dir():
        parser.error("--directory must be an existing local directory")
    initialize_stream(dsn, stream_id=args.stream, namespace_id=args.namespace)
    seen: dict[str, tuple[int, int, int]] = {}
    while True:
        try:
            outcomes = scan_directory(
                args.directory,
                dsn=dsn,
                stream_id=args.stream,
                namespace_id=args.namespace,
                seen=seen,
                max_files=args.max_files_per_scan,
            )
            for outcome in outcomes:
                print(json.dumps(outcome, ensure_ascii=False, sort_keys=True), flush=True)
            if not args.watch:
                if any(item["status"] not in ("imported", "repeated") for item in outcomes):
                    raise SystemExit(1)
                return
        except (psycopg.Error, OSError):
            if not args.watch:
                raise
            print(json.dumps({"status": "inbox_or_storage_unavailable"}), flush=True)
        try:
            time.sleep(args.poll_seconds)
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    main()

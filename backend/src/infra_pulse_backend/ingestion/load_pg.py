"""Write parsed files into PostgreSQL. Every function runs inside the caller's
transaction: either the whole file is accepted or nothing is (no half-loaded file).

Journal records go to ``dispatch_observations`` of the canonical received scope
with ``availability_basis='observed'`` and ``available_at`` = import commit time.
``row_uid`` is derived from the overlap key (channel, time, event ID, value), so
a record repeated inside a file or across overlapping files is stored once. A stored
record is also matched by the overlap key itself (channel index, event ID compared
with ``IS NOT DISTINCT FROM``): the history seeded by
``backend/scripts/seed_forecast_history.py`` keeps the row_uid of the curated snapshot
(``sha256(source_sha256:record_ordinal)``), and the same record sent again through a
file or an API batch is a duplicate, not a second row (rehearsal 29.09.2026, P0-1).

A record without an alarm column (ТЗ Appendix 1 layout, ``JournalRow.alarm is None``)
is stored with ``alarm = NULL`` — «не передан источником», never ``false`` and never
derived from the text (migration 0018). ``alarm_count`` and ``alarms_accepted`` count
only records whose source said ``true``. When a record with the same overlap key
arrives later with a filled flag (organizers' export, API batch), the flag replaces
the NULL; a NULL never overwrites ``true``/``false``.

For the Appendix 1 layout the sensor type still comes from the channel reference;
«ИД типа канала данных» is kept verbatim in ``source_channel_type_id`` and a record
whose ID contradicts the reference is quarantined as ``channel_type_conflict``
(``channel_types``). «Дата записи» has minute precision, so inside a minute the load
order (``received_position``) follows «ИД записи журнала» (numeric IDs as numbers).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.rows import tuple_row

from infra_pulse_backend.ingestion.channel_types import contradicts
from infra_pulse_backend.ingestion.csv_source import Quarantined
from infra_pulse_backend.ingestion.journal_csv import (
    LAYOUT_TZ,
    JournalRow,
    ParsedJournal,
    dedup_identity,
)
from infra_pulse_backend.ingestion.reference_csv import ParsedReference

ROW_UID_VERSION = "journal-csv-v1"
_REF_TABLES = {
    "channels": (
        "ref_channels",
        "(version_id, line_no, channel_id, system_type, sensor_type, tag, name, object_id)",
    ),
    "objects": (
        "ref_objects",
        "(version_id, line_no, object_id, level_raw, parent_raw, object_kind, name)",
    ),
    "states": ("ref_states", "(version_id, line_no, sensor_type, state_set_id, state_name, alarm)"),
}


def _q(connection: psycopg.Connection) -> psycopg.Cursor:
    """Tuple rows whatever row factory the caller's connection uses."""
    return connection.cursor(row_factory=tuple_row)


class ReferenceMissing(LookupError):
    """No channel reference has been loaded; the journal cannot be mapped."""


class NoValidRows(ValueError):
    """Every record was quarantined at load (type conflicts); nothing was written."""


@dataclass
class JournalLoad:
    rows_total: int
    rows_accepted: int
    rows_duplicate: int
    rows_quarantined: int
    unknown_channels: int
    alarms_accepted: int
    event_from: datetime | None
    event_to: datetime | None
    reference_version: str
    received_at: datetime
    quarantine_reasons: dict[str, int] = field(default_factory=dict)
    # Duplicates whose stored NULL alarm was filled by this file's flag.
    alarms_filled: int = 0


def row_uid(row: JournalRow) -> str:
    """Deterministic ID of the overlap key, prefixed by the UTC event day.

    The prefix keeps a day's records together in the primary-key index, so loading a
    new day touches new index pages instead of random pages across the whole history.
    """
    digest = hashlib.sha256(f"{ROW_UID_VERSION}\x00{dedup_identity(row)}".encode()).hexdigest()
    return f"csv-{row.event_at.astimezone(UTC):%Y%m%d}-{digest[:40]}"


def active_reference(connection: psycopg.Connection, kind: str) -> str | None:
    row = (
        _q(connection)
        .execute(
            """SELECT version_id FROM ref_versions WHERE kind = %s
           ORDER BY activation_seq DESC LIMIT 1""",
            (kind,),
        )
        .fetchone()
    )
    return row[0] if row else None


def channel_map(
    connection: psycopg.Connection, version_id: str
) -> dict[str, tuple[str | None, str, str] | None]:
    """channel -> (object, sensor type, system type); None when listed ambiguously."""
    mapping: dict[str, tuple[str | None, str, str] | None] = {}
    rows = _q(connection).execute(
        """SELECT channel_id, object_id, sensor_type, system_type
           FROM ref_channels WHERE version_id = %s""",
        (version_id,),
    )
    for channel_id, object_id, sensor_type, system_type in rows:
        value = (object_id, sensor_type, system_type)
        if channel_id in mapping and mapping[channel_id] != value:
            mapping[channel_id] = None
        else:
            mapping.setdefault(channel_id, value)
    return mapping


def lock_received_scope(
    connection: psycopg.Connection, *, namespace_id: str, stream_id: str
) -> tuple[int, datetime]:
    """Create the received scope if needed, lock it and return (row_count, received_at).

    Every writer of the scope (this worker and the legacy JSON watcher) locks the
    scope row first, so committed ``received_position`` values stay dense.
    """
    at = _q(connection).execute("SELECT clock_timestamp()").fetchone()[0]
    _q(connection).execute(
        """INSERT INTO dispatch_replay_snapshots
           (namespace_id, snapshot_id, manifest_sha256, window_start,
            window_end, row_count, alarm_count, scope_kind)
           VALUES (%s, %s, NULL, %s, %s, 0, 0, 'received')
           ON CONFLICT (namespace_id, snapshot_id) DO NOTHING""",
        (namespace_id, stream_id, at - timedelta(microseconds=1), at + timedelta(microseconds=1)),
    )
    scope = (
        _q(connection)
        .execute(
            """SELECT scope_kind, row_count FROM dispatch_replay_snapshots
           WHERE namespace_id = %s AND snapshot_id = %s FOR UPDATE""",
            (namespace_id, stream_id),
        )
        .fetchone()
    )
    if scope is None or scope[0] != "received":
        raise ValueError("received namespace and stream collide with a replay scope")
    # Timestamp after the lock: import order and available_at never disagree.
    received_at = _q(connection).execute("SELECT clock_timestamp()").fetchone()[0]
    return scope[1], received_at


def save_quarantine(
    connection: psycopg.Connection, import_id: str, rows: list[Quarantined]
) -> dict[str, int]:
    if rows:
        with connection.cursor() as cursor:
            with cursor.copy(
                """COPY import_quarantine (import_id, line_no, record_ordinal, reason, raw_cells)
                   FROM STDIN"""
            ) as copy:
                for row in rows:
                    copy.write_row(
                        (import_id, row.line_no, row.record_ordinal, row.reason, row.raw_json())
                    )
    return dict(Counter(row.reason for row in rows))


def load_journal(
    connection: psycopg.Connection,
    *,
    import_id: str,
    parsed: ParsedJournal,
    file_name: str,
    sha256: str,
    namespace_id: str,
    stream_id: str,
) -> JournalLoad:
    reference_version = active_reference(connection, "channels")
    if reference_version is None:
        raise ReferenceMissing("load the channel reference before the journal")
    mapping = channel_map(connection, reference_version)
    if parsed.layout == LAYOUT_TZ.name:
        _quarantine_type_conflicts(parsed, mapping)
        if not parsed.rows:
            raise NoValidRows("every record contradicts the channel reference type")
    prior_rows, received_at = lock_received_scope(
        connection, namespace_id=namespace_id, stream_id=stream_id
    )
    _q(connection).execute(
        """CREATE TEMP TABLE import_stage (
             row_uid text NOT NULL, record_ordinal bigint NOT NULL,
             source_event_id text, channel_id text NOT NULL, object_id text,
             sensor_type text, system_type text, value_raw text NOT NULL,
             value_numeric double precision, alarm boolean,
             event_at timestamptz NOT NULL, event_local_raw text NOT NULL,
             quality_flags jsonb NOT NULL, source_channel_type_id text,
             load_order bigint NOT NULL
           ) ON COMMIT DROP"""
    )
    # Sorts of a daily file stay in memory; the setting ends with the transaction.
    _q(connection).execute("SET LOCAL work_mem = '64MB'")
    unknown: set[str] = set()
    uids: list[str] = []
    at_from = min(row.event_at for row in parsed.rows)
    at_to = max(row.event_at for row in parsed.rows)
    with connection.cursor() as cursor:
        with cursor.copy(
            """COPY import_stage (row_uid, record_ordinal, source_event_id, channel_id,
                 object_id, sensor_type, system_type, value_raw, value_numeric, alarm,
                 event_at, event_local_raw, quality_flags, source_channel_type_id,
                 load_order)
               FROM STDIN"""
        ) as copy:
            for row, load_order in zip(parsed.rows, _load_order(parsed), strict=True):
                flags = []
                if row.is_epoch_placeholder:
                    flags.append("is_epoch_placeholder")
                reference = mapping.get(row.channel_id)
                if reference is None:
                    unknown.add(row.channel_id)
                    flags.append(
                        "reference_conflict" if row.channel_id in mapping else "reference_unmatched"
                    )
                    object_id = sensor_type = system_type = None
                else:
                    object_id, sensor_type, system_type = reference
                uid = row_uid(row)
                uids.append(uid)
                copy.write_row(
                    (
                        uid,
                        row.record_ordinal,
                        row.event_id,
                        row.channel_id,
                        object_id,
                        sensor_type,
                        system_type,
                        row.value_raw,
                        row.value_numeric,
                        row.alarm,
                        row.event_at,
                        row.event_local_raw,
                        json.dumps(flags),
                        row.channel_type_id,
                        load_order,
                    )
                )
    _q(connection).execute("ANALYZE import_stage")
    inserted, alarms = (
        _q(connection)
        .execute(
            """WITH firsts AS (
             SELECT DISTINCT ON (row_uid) * FROM import_stage
             ORDER BY row_uid, record_ordinal
           ), fresh AS (
             SELECT firsts.*, row_number() OVER (ORDER BY load_order) AS position
             FROM firsts
             WHERE NOT EXISTS (
               SELECT 1 FROM dispatch_observations AS observation
               WHERE observation.namespace_id = %(namespace)s
                 AND observation.snapshot_id = %(stream)s
                 AND observation.row_uid BETWEEN %(uid_from)s AND %(uid_to)s
                 AND observation.row_uid = firsts.row_uid
             )
             -- The overlap key itself: the seeded history keeps the row_uid of the
             -- curated snapshot (see module docstring), so the ID alone misses it.
             AND NOT EXISTS (
               SELECT 1 FROM dispatch_observations AS observation
               WHERE observation.namespace_id = %(namespace)s
                 AND observation.snapshot_id = %(stream)s
                 AND observation.event_at BETWEEN %(at_from)s AND %(at_to)s
                 AND observation.channel_id = firsts.channel_id
                 AND observation.event_at = firsts.event_at
                 AND observation.source_event_id IS NOT DISTINCT FROM firsts.source_event_id
                 AND observation.value_raw = firsts.value_raw
             )
           ), inserted AS (
             INSERT INTO dispatch_observations
               (namespace_id, snapshot_id, row_uid, source_event_id, channel_id,
                object_id, sensor_type, system_type, value_raw, value_numeric, alarm,
                event_at, available_at, availability_basis, source_file, source_sha256,
                record_ordinal, event_local_raw, reference_version, quality_flags,
                received_position, source_channel_type_id)
             SELECT %(namespace)s, %(stream)s, row_uid, source_event_id, channel_id,
                    object_id, sensor_type, system_type, value_raw, value_numeric, alarm,
                    event_at, %(received_at)s, 'observed', %(file_name)s, %(sha256)s,
                    record_ordinal, event_local_raw, %(reference_version)s,
                    CASE WHEN event_at > %(received_at)s
                         THEN quality_flags || '["source_clock_ahead_of_import"]'::jsonb
                         ELSE quality_flags END,
                    %(prior_rows)s + position, source_channel_type_id
             FROM fresh
             -- Physical order by channel: the channel indexes get one burst of
             -- writes per channel instead of random page writes across history.
             ORDER BY object_id, system_type, channel_id, event_at DESC
             RETURNING alarm
           )
           SELECT count(*), count(*) FILTER (WHERE alarm) FROM inserted""",
            {
                "namespace": namespace_id,
                "stream": stream_id,
                "received_at": received_at,
                "file_name": file_name,
                "sha256": sha256,
                "reference_version": reference_version,
                "prior_rows": prior_rows,
                # Day-prefixed IDs: only the file's days of history are probed.
                "uid_from": min(uids),
                "uid_to": max(uids),
                # The same for the overlap key: a large file reads its own period only.
                "at_from": at_from,
                "at_to": at_to,
            },
        )
        .fetchone()
    )
    # A filled flag completes a stored «не передан» (NULL); NULL never overwrites.
    # Matched by the overlap key like the duplicates above, whatever the stored row_uid.
    filled, filled_alarms = (
        _q(connection)
        .execute(
            """WITH incoming AS (
                 SELECT DISTINCT ON (row_uid) channel_id, event_at, source_event_id,
                        value_raw, alarm
                 FROM import_stage
                 WHERE alarm IS NOT NULL ORDER BY row_uid, record_ordinal
               ), filled AS (
                 UPDATE dispatch_observations AS observation
                 SET alarm = incoming.alarm
                 FROM incoming
                 WHERE observation.namespace_id = %(namespace)s
                   AND observation.snapshot_id = %(stream)s
                   AND observation.event_at BETWEEN %(at_from)s AND %(at_to)s
                   AND observation.channel_id = incoming.channel_id
                   AND observation.event_at = incoming.event_at
                   AND observation.source_event_id IS NOT DISTINCT FROM incoming.source_event_id
                   AND observation.value_raw = incoming.value_raw
                   AND observation.alarm IS NULL
                 RETURNING observation.alarm
               )
               SELECT count(*), count(*) FILTER (WHERE alarm) FROM filled""",
            {"namespace": namespace_id, "stream": stream_id, "at_from": at_from, "at_to": at_to},
        )
        .fetchone()
    )
    if filled_alarms:
        _q(connection).execute(
            """UPDATE dispatch_replay_snapshots SET alarm_count = alarm_count + %s
               WHERE namespace_id = %s AND snapshot_id = %s""",
            (filled_alarms, namespace_id, stream_id),
        )
    if inserted:
        _q(connection).execute(
            """UPDATE dispatch_replay_snapshots
               SET row_count = row_count + %s, alarm_count = alarm_count + %s,
                   last_received_at = %s, window_end = GREATEST(window_end, %s)
               WHERE namespace_id = %s AND snapshot_id = %s""",
            (
                inserted,
                alarms,
                received_at,
                received_at + timedelta(microseconds=1),
                namespace_id,
                stream_id,
            ),
        )
    reasons = save_quarantine(connection, import_id, parsed.quarantined)
    period = parsed.period()
    return JournalLoad(
        rows_total=parsed.records,
        rows_accepted=inserted,
        rows_duplicate=len(parsed.rows) - inserted,
        rows_quarantined=len(parsed.quarantined),
        unknown_channels=len(unknown),
        alarms_accepted=alarms,
        event_from=period[0] if period else None,
        event_to=period[1] if period else None,
        reference_version=reference_version,
        received_at=received_at,
        quarantine_reasons=reasons,
        alarms_filled=filled,
    )


def _numeric_first(event_id: str | None) -> tuple[int, int, str]:
    text = event_id or ""
    return (0, int(text), text) if text.isascii() and text.isdigit() else (1, 0, text)


def _load_order(parsed: ParsedJournal) -> list[int]:
    """Position of each row in the load: file order, except Appendix 1 (see module)."""
    if parsed.layout != LAYOUT_TZ.name:
        return [row.record_ordinal for row in parsed.rows]
    ranked = sorted(
        range(len(parsed.rows)),
        key=lambda index: (
            parsed.rows[index].event_at,
            _numeric_first(parsed.rows[index].event_id),
            parsed.rows[index].record_ordinal,
        ),
    )
    order = [0] * len(ranked)
    for rank, index in enumerate(ranked, start=1):
        order[index] = rank
    return order


def _quarantine_type_conflicts(
    parsed: ParsedJournal, mapping: dict[str, tuple[str | None, str, str] | None]
) -> None:
    """Move Appendix 1 records whose type ID contradicts the reference to quarantine."""
    kept: list[JournalRow] = []
    for row in parsed.rows:
        reference = mapping.get(row.channel_id)
        if reference is not None and contradicts(row.channel_type_id, reference[1]):
            cells = [
                row.event_id or "",
                row.channel_id,
                row.channel_type_id or "",
                row.value_raw,
                row.event_local_raw,
            ]
            parsed.quarantined.append(
                Quarantined(row.line_no, row.record_ordinal, "channel_type_conflict", cells)
            )
        else:
            kept.append(row)
    if len(kept) != len(parsed.rows):
        parsed.rows[:] = kept
        parsed.quarantined.sort(key=lambda item: item.line_no)


def reference_version_id(kind: str, sha256: str) -> str:
    return f"{kind}-{sha256[:16]}"


def load_reference(
    connection: psycopg.Connection,
    *,
    import_id: str,
    parsed: ParsedReference,
    sha256: str,
) -> str:
    """Store a new reference version and make it the active one of its kind."""
    version_id = reference_version_id(parsed.kind, sha256)
    _q(connection).execute(
        """INSERT INTO ref_versions (version_id, kind, sha256, import_id, row_count)
           VALUES (%s, %s, %s, %s, %s)""",
        (version_id, parsed.kind, sha256, import_id, len(parsed.rows)),
    )
    table, columns = _REF_TABLES[parsed.kind]
    with connection.cursor() as cursor:
        with cursor.copy(f"COPY {table} {columns} FROM STDIN") as copy:
            for row in parsed.rows:
                copy.write_row((version_id, *row))
    save_quarantine(connection, import_id, parsed.quarantined)
    return version_id

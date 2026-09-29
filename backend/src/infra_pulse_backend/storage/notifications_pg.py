"""PostgreSQL reads of critical source records for notifications (N1, policy v1).

One read-only REPEATABLE READ transaction reads every candidate record of the policy in
``(since - LOOKBACK, data_as_of]`` (the lookback gives a series its neighbours before
``since`` and an hour of repeats of one channel its start), the «Обесточен»/«Неисправен»
records around pump «Затоплен» (rule (а)), the current object names and the channel names of
the listed records (the channel layout, as ``/attention`` shows them). Rows are built
in ``operations.notifications.alarm_rows``; the exact count is the number of rows whose
newest record lies in ``(since, data_as_of]``. Records later than the stand clock are
never read: ``event_at <= data_as_of`` and ``available_at <= available_as_of`` (see
``operations.notifications.StandClock``). No migration: two scans of the window stay
within the latency target on 100 000 rows of one scope (backend/README.md, «Уведомления»).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

from infra_pulse_backend.operations.notifications import (
    CRITICAL_PAIRS,
    LOOKBACK,
    POWER_TEXTS,
    POWER_WINDOW,
    PUMP_PAIRS,
    AlarmRecord,
    AlarmRow,
    PowerEvent,
    alarm_rows,
    selection_key,
    window_rows,
    with_power_context,
)
from infra_pulse_backend.storage.attention_pg import read_channel_pickets
from infra_pulse_core.contracts.notifications import MAX_ROW_MEMBERS

CRITICAL_CLAUSE = (
    "(sensor_type, value_raw) IN (" + ", ".join(["(%s, %s)"] * len(CRITICAL_PAIRS)) + ")"
)
CRITICAL_PARAMS = tuple(value for pair in CRITICAL_PAIRS for value in pair)
_COLUMNS = "row_uid, channel_id, object_id, sensor_type, value_raw, alarm, event_at"


@dataclass(frozen=True)
class CriticalAlarmRead:
    """``total`` rows in the window, ``records`` the rows selected for the list
    (``selection_key``, at most ``limit``); ``test_rows`` stays empty since v1."""

    total: int
    records: list[AlarmRow]
    test_rows: set[str]
    object_names: dict[str, str]


def read_object_names(
    connection: psycopg.Connection, object_ids: Iterable[str | None]
) -> dict[str, str]:
    """Names of objects in the current objects reference (B1); unknown IDs are absent."""
    wanted = sorted({object_id for object_id in object_ids if object_id})
    if not wanted:
        return {}
    rows = connection.cursor(row_factory=dict_row).execute(
        """SELECT DISTINCT ON (object_id) object_id, name
           FROM ref_objects
           WHERE version_id = (
             SELECT version_id FROM ref_versions WHERE kind = 'objects'
             ORDER BY activation_seq DESC LIMIT 1
           )
             AND object_id = ANY(%s)
           ORDER BY object_id, line_no""",
        (wanted,),
    )
    return {row["object_id"]: row["name"] for row in rows}


def read_channel_names(
    connection: psycopg.Connection, channel_ids: Iterable[str]
) -> dict[str, str]:
    """Channel names of the channel layout, as ``/attention`` shows them (QA 29.09, D3);
    channels without a layout row or a name are absent."""
    known = read_channel_pickets(connection, sorted(set(channel_ids)))
    return {
        channel_id: values["channel_name"]
        for channel_id, values in known.items()
        if values["channel_name"]
    }


def _named(row: AlarmRow, objects: dict[str, str], channels: dict[str, str]) -> AlarmRow:
    """The row with its object name and the channel names of its records."""
    return replace(
        row,
        object_name=objects.get(row.object_id or ""),
        members=tuple(
            replace(record, channel_name=channels.get(record.channel_id)) for record in row.members
        ),
    )


def _record(row: dict) -> AlarmRecord:
    return AlarmRecord(
        row_uid=row["row_uid"],
        channel_id=row["channel_id"],
        object_id=row["object_id"],
        sensor_type=row["sensor_type"],
        value_raw=row["value_raw"],
        alarm=row["alarm"],
        event_at=row["event_at"],
    )


def _power_events(
    connection: psycopg.Connection,
    pumps: list[AlarmRecord],
    *,
    namespace_id: str,
    snapshot_id: str,
    data_as_of: datetime,
    available_as_of: datetime,
) -> list[PowerEvent]:
    """Power-loss records of the pumps' objects (channels without object) around them."""
    if not pumps:
        return []
    first = min(record.event_at for record in pumps) - POWER_WINDOW
    last = min(max(record.event_at for record in pumps) + POWER_WINDOW, data_as_of)
    objects = sorted({record.object_id for record in pumps if record.object_id is not None})
    channels = sorted({record.channel_id for record in pumps if record.object_id is None})
    rows = connection.execute(
        """SELECT object_id, channel_id, event_at FROM dispatch_observations
           WHERE namespace_id = %s AND snapshot_id = %s
             AND value_raw = ANY(%s)
             AND event_at >= %s AND event_at <= %s AND available_at <= %s
             AND (object_id = ANY(%s) OR (object_id IS NULL AND channel_id = ANY(%s)))""",
        (
            namespace_id,
            snapshot_id,
            list(POWER_TEXTS),
            first,
            last,
            available_as_of,
            objects,
            channels,
        ),
    ).fetchall()
    return [PowerEvent(row["object_id"], row["channel_id"], row["event_at"]) for row in rows]


def read_critical_alarms(
    dsn: str,
    *,
    namespace_id: str,
    snapshot_id: str,
    mode: str,
    since: datetime,
    data_as_of: datetime,
    available_as_of: datetime,
    limit: int = 50,
    extra_object_ids: Iterable[str | None] = (),
) -> CriticalAlarmRead:
    """Exact number of rows and the ``limit`` rows to list of one observation scope."""
    for moment in (since, data_as_of, available_as_of):
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("notification bounds must be timezone-aware")
    if not 1 <= limit <= 100:
        raise ValueError("invalid notification limit")
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        scope = connection.execute(
            """SELECT 1 FROM dispatch_replay_snapshots
               WHERE namespace_id = %s AND snapshot_id = %s AND scope_kind = %s""",
            (namespace_id, snapshot_id, mode),
        ).fetchone()
        if scope is None:
            raise LookupError("observation scope not loaded")
        candidates = [
            _record(row)
            for row in connection.execute(
                f"""SELECT {_COLUMNS} FROM dispatch_observations
                    WHERE namespace_id = %s AND snapshot_id = %s
                      AND event_at > %s AND event_at <= %s AND available_at <= %s
                      AND {CRITICAL_CLAUSE}""",
                (
                    namespace_id,
                    snapshot_id,
                    since - LOOKBACK,
                    data_as_of,
                    available_as_of,
                    *CRITICAL_PARAMS,
                ),
            )
        ]
        pumps = [
            record for record in candidates if (record.sensor_type, record.value_raw) in PUMP_PAIRS
        ]
        power = _power_events(
            connection,
            pumps,
            namespace_id=namespace_id,
            snapshot_id=snapshot_id,
            data_as_of=data_as_of,
            available_as_of=available_as_of,
        )
        rows = window_rows(
            alarm_rows(with_power_context(candidates, power)), since=since, data_as_of=data_as_of
        )
        selected = sorted(rows, key=selection_key)[:limit]
        names = read_object_names(
            connection, [*(row.object_id for row in selected), *extra_object_ids]
        )
        channels = read_channel_names(
            connection,
            (record.channel_id for row in selected for record in row.members[-MAX_ROW_MEMBERS:]),
        )
    return CriticalAlarmRead(
        total=len(rows),
        records=[_named(row, names, channels) for row in selected],
        test_rows=set(),
        object_names=names,
    )

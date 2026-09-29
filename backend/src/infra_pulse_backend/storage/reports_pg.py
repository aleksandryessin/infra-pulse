"""PostgreSQL reads for management reports (R1): source alarms of a month and names.

Cards, outcomes and decisions come from the forecast journal (``forecast_db``, B2); this
module reads only the observation scope. One read-only REPEATABLE READ transaction counts
records with the source ``alarm=true`` whose event time lies in ``[start, end)`` and not
later than the data watermark, and whose availability is not later than
``available_as_of`` (see ``operations.notifications.StandClock``). Nothing else is read:
no notes, sessions or audit rows.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

from infra_pulse_backend.operations.reports import UNKNOWN_SENSOR_TYPE, AlarmCounts
from infra_pulse_backend.storage.notifications_pg import read_object_names


def read_source_alarm_counts(
    dsn: str,
    *,
    namespace_id: str,
    snapshot_id: str,
    mode: str,
    start: datetime,
    end: datetime,
    data_as_of: datetime,
    available_as_of: datetime,
) -> AlarmCounts:
    for moment in (start, end, data_as_of, available_as_of):
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("report bounds must be timezone-aware")
    if start >= end:
        raise ValueError("report period is empty or reversed")
    window = (namespace_id, snapshot_id, start, end, data_as_of, available_as_of)
    where = """namespace_id = %s AND snapshot_id = %s AND alarm
               AND event_at >= %s AND event_at < %s AND event_at <= %s
               AND available_at <= %s"""
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        scope = connection.execute(
            """SELECT 1 FROM dispatch_replay_snapshots
               WHERE namespace_id = %s AND snapshot_id = %s AND scope_kind = %s""",
            (namespace_id, snapshot_id, mode),
        ).fetchone()
        if scope is None:
            raise LookupError("observation scope not loaded")
        by_type = connection.execute(
            f"""SELECT sensor_type, count(*) AS n FROM dispatch_observations
                WHERE {where} GROUP BY sensor_type""",
            window,
        ).fetchall()
        by_object = connection.execute(
            f"""SELECT object_id, count(*) AS n FROM dispatch_observations
                WHERE {where} AND object_id IS NOT NULL GROUP BY object_id""",
            window,
        ).fetchall()
    types: dict[str, int] = {}
    for row in by_type:
        key = row["sensor_type"] or UNKNOWN_SENSOR_TYPE
        types[key] = types.get(key, 0) + row["n"]
    return AlarmCounts(
        by_type=types,
        by_object={row["object_id"]: row["n"] for row in by_object},
    )


def read_names(dsn: str, object_ids: Iterable[str | None]) -> dict[str, str]:
    """Current reference names of the given objects (a short read-only transaction)."""
    wanted = [object_id for object_id in object_ids if object_id]
    if not wanted:
        return {}
    with psycopg.connect(dsn) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        return read_object_names(connection, wanted)

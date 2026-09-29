"""Reference coverage for bounded replay; silence is never interpreted as health."""

from datetime import datetime

import psycopg
from psycopg.rows import dict_row

from infra_pulse_core.contracts.attention import (
    ReplayCoverageChannel,
    ReplayCoverageChannelList,
    ReplayCoverageObject,
    ReplayCoverageObjectList,
)


def _scope(connection: psycopg.Connection, namespace_id: str, snapshot_id: str) -> tuple:
    row = connection.execute(
        """SELECT scope.window_start, min(roster.reference_sha256) AS reference_sha256,
                  max(roster.reference_sha256) AS max_reference_sha256, count(*) AS channels
           FROM dispatch_replay_snapshots AS scope
           JOIN dispatch_replay_channel_roster AS roster
             ON roster.namespace_id = scope.namespace_id
            AND roster.snapshot_id = scope.snapshot_id
           WHERE scope.namespace_id = %s AND scope.snapshot_id = %s
             AND scope.scope_kind = 'replay'
           GROUP BY scope.window_start""",
        (namespace_id, snapshot_id),
    ).fetchone()
    if row is None or row["reference_sha256"] != row["max_reference_sha256"]:
        raise LookupError("replay reference roster not loaded")
    return row["window_start"], row["reference_sha256"]


def read_coverage_objects(
    dsn: str,
    *,
    namespace_id: str,
    snapshot_id: str,
    as_of: datetime,
) -> ReplayCoverageObjectList:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        window_start, reference_sha = _scope(connection, namespace_id, snapshot_id)
        rows = connection.execute(
            """WITH heard AS (
                 SELECT channel_id, count(*) AS record_count,
                        max(available_at) AS last_available_at
                 FROM dispatch_observations
                 WHERE namespace_id = %s AND snapshot_id = %s
                   AND available_at >= %s AND available_at <= %s
                 GROUP BY channel_id
               )
               SELECT roster.object_id,
                      count(*) AS reference_channel_count,
                      count(heard.channel_id) AS heard_channel_count,
                      coalesce(sum(heard.record_count), 0) AS record_count,
                      max(heard.last_available_at) AS last_available_at
               FROM dispatch_replay_channel_roster AS roster
               LEFT JOIN heard ON heard.channel_id = roster.channel_id
               WHERE roster.namespace_id = %s AND roster.snapshot_id = %s
               GROUP BY roster.object_id
               ORDER BY roster.object_id NULLS LAST""",
            (namespace_id, snapshot_id, window_start, as_of, namespace_id, snapshot_id),
        ).fetchall()
    items = [ReplayCoverageObject.model_validate(row) for row in rows]
    return ReplayCoverageObjectList(
        as_of=as_of,
        window_start=window_start,
        reference_sha256=reference_sha,
        items=items,
        total=len(items),
    )


def read_coverage_channels(
    dsn: str,
    *,
    namespace_id: str,
    snapshot_id: str,
    as_of: datetime,
    object_id: str,
    offset: int,
    limit: int,
) -> ReplayCoverageChannelList:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("invalid coverage page")
    object_clause = (
        "roster.object_id IS NULL" if object_id == "__unknown__" else "roster.object_id = %s"
    )
    scope = (namespace_id, snapshot_id)
    if object_id != "__unknown__":
        scope = (*scope, object_id)
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        window_start, reference_sha = _scope(connection, namespace_id, snapshot_id)
        total = connection.execute(
            f"""SELECT count(*) AS n FROM dispatch_replay_channel_roster AS roster
                WHERE roster.namespace_id = %s AND roster.snapshot_id = %s
                  AND {object_clause}""",
            scope,
        ).fetchone()["n"]
        rows = connection.execute(
            f"""WITH heard AS (
                 SELECT channel_id, count(*) AS record_count,
                        max(available_at) AS last_available_at
                 FROM dispatch_observations
                 WHERE namespace_id = %s AND snapshot_id = %s
                   AND available_at >= %s AND available_at <= %s
                 GROUP BY channel_id
               )
               SELECT roster.channel_id, roster.object_id, roster.system_type,
                      roster.sensor_type,
                      coalesce(heard.record_count, 0) AS record_count,
                      heard.last_available_at
               FROM dispatch_replay_channel_roster AS roster
               LEFT JOIN heard ON heard.channel_id = roster.channel_id
               WHERE roster.namespace_id = %s AND roster.snapshot_id = %s
                 AND {object_clause}
               ORDER BY (heard.channel_id IS NOT NULL),
                        roster.system_type NULLS LAST, roster.channel_id
               LIMIT %s OFFSET %s""",
            (
                namespace_id,
                snapshot_id,
                window_start,
                as_of,
                *scope,
                limit,
                offset,
            ),
        ).fetchall()
    items = [
        ReplayCoverageChannel(
            **row,
            coverage_status=(
                "heard_in_replay_window" if row["record_count"] else "no_record_in_replay_window"
            ),
        )
        for row in rows
    ]
    return ReplayCoverageChannelList(
        as_of=as_of,
        window_start=window_start,
        reference_sha256=reference_sha,
        object_id=None if object_id == "__unknown__" else object_id,
        items=items,
        offset=offset,
        total=total,
    )

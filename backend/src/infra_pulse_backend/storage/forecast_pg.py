"""PostgreSQL storage of the phase-feeder forecast (migration 0013).

Every function runs inside the caller's connection/transaction and uses tuple rows, so
it works on the worker's autocommit connection wrapped in ``connection.transaction()``
and on the API's read-only transaction alike. Tables are scoped by the observation scope
``(namespace_id, snapshot_id)``; nothing here imports pandas, numpy or research code.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import psycopg
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from infra_pulse_core.features.phase_feeder_episodes import (
    DETECTOR_VERSION,
    MSK,
    PHASE_SENSOR_TYPE,
    ChannelState,
    Episode,
    PhaseEvent,
)


def _q(connection: psycopg.Connection) -> psycopg.Cursor:
    return connection.cursor(row_factory=tuple_row)


@dataclass(frozen=True)
class Scope:
    namespace_id: str
    snapshot_id: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.namespace_id, self.snapshot_id)


@dataclass
class ScopeRow:
    mode: str
    generation: int
    detector_position: int
    list_from: date | None
    last_cutoff: datetime | None
    data_as_of: datetime | None
    next_journal_position: int


@dataclass(frozen=True)
class LayoutRow:
    channel_id: str
    object_id: str | None
    sensor_type: str | None
    system_type: str | None
    name: str | None
    tag: str | None
    role: str | None
    feeder_kind: str | None
    landmark_kind: str | None
    picket_form: str
    picket_from: float | None
    picket_to: float | None
    picket_basis: str | None


def table_exists(connection: psycopg.Connection, name: str) -> bool:
    return _q(connection).execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()[0]


# -- scope ----------------------------------------------------------------------------------


def scope_mode(connection: psycopg.Connection, scope: Scope) -> str | None:
    row = (
        _q(connection)
        .execute(
            """SELECT scope_kind FROM dispatch_replay_snapshots
               WHERE namespace_id = %s AND snapshot_id = %s""",
            scope.key,
        )
        .fetchone()
    )
    return None if row is None else row[0]


def lock_scope(connection: psycopg.Connection, scope: Scope, mode: str) -> ScopeRow:
    """Create and lock the forecast scope row: one publisher at a time."""
    _q(connection).execute(
        """INSERT INTO forecast_scopes (namespace_id, snapshot_id, mode) VALUES (%s, %s, %s)
           ON CONFLICT (namespace_id, snapshot_id) DO NOTHING""",
        (*scope.key, mode),
    )
    row = (
        _q(connection)
        .execute(
            """SELECT mode, generation, detector_position, list_from, last_cutoff, data_as_of,
                      next_journal_position
               FROM forecast_scopes WHERE namespace_id = %s AND snapshot_id = %s FOR UPDATE""",
            scope.key,
        )
        .fetchone()
    )
    return ScopeRow(*row)


def read_scope(connection: psycopg.Connection, scope: Scope) -> ScopeRow | None:
    if not table_exists(connection, "forecast_scopes"):
        return None
    row = (
        _q(connection)
        .execute(
            """SELECT mode, generation, detector_position, list_from, last_cutoff, data_as_of,
                      next_journal_position
               FROM forecast_scopes WHERE namespace_id = %s AND snapshot_id = %s""",
            scope.key,
        )
        .fetchone()
    )
    return None if row is None else ScopeRow(*row)


def update_scope(connection: psycopg.Connection, scope: Scope, **fields) -> None:
    columns = ", ".join(f"{name} = %s" for name in fields)
    _q(connection).execute(
        f"UPDATE forecast_scopes SET {columns} WHERE namespace_id = %s AND snapshot_id = %s",
        (*fields.values(), *scope.key),
    )


# -- layout -----------------------------------------------------------------------------------


def upsert_layout(connection: psycopg.Connection, rows: Iterable[dict]) -> int:
    rows = list(rows)
    with connection.cursor() as cursor:
        cursor.executemany(
            """INSERT INTO forecast_channel_layout
                     (channel_id, object_id, sensor_type, system_type, name, tag, role,
                      feeder_kind, landmark_kind, picket_form, picket_from, picket_to,
                      picket_basis, layout_version, reference_version)
                   VALUES (%(channel_id)s, %(object_id)s, %(sensor_type)s, %(system_type)s,
                           %(name)s, %(tag)s, %(role)s, %(feeder_kind)s, %(landmark_kind)s,
                           %(picket_form)s, %(picket_from)s, %(picket_to)s, %(picket_basis)s,
                           %(layout_version)s, %(reference_version)s)
                   ON CONFLICT (channel_id) DO UPDATE SET
                     object_id = EXCLUDED.object_id, sensor_type = EXCLUDED.sensor_type,
                     system_type = EXCLUDED.system_type, name = EXCLUDED.name,
                     tag = EXCLUDED.tag, role = EXCLUDED.role,
                     feeder_kind = EXCLUDED.feeder_kind, landmark_kind = EXCLUDED.landmark_kind,
                     picket_form = EXCLUDED.picket_form, picket_from = EXCLUDED.picket_from,
                     picket_to = EXCLUDED.picket_to, picket_basis = EXCLUDED.picket_basis,
                     layout_version = EXCLUDED.layout_version,
                     reference_version = EXCLUDED.reference_version,
                     updated_at = clock_timestamp()""",
            rows,
        )
    return len(rows)


def upsert_objects(connection: psycopg.Connection, rows: Iterable[tuple[str, str | None, str]]):
    with connection.cursor() as cursor:
        for object_id, name, version in rows:
            cursor.execute(
                """INSERT INTO forecast_objects (object_id, object_name, reference_version)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (object_id) DO UPDATE SET object_name = EXCLUDED.object_name,
                     reference_version = EXCLUDED.reference_version""",
                (object_id, name, version),
            )


def layout_reference_versions(connection: psycopg.Connection) -> set[str]:
    rows = (
        _q(connection)
        .execute("SELECT DISTINCT reference_version FROM forecast_channel_layout")
        .fetchall()
    )
    return {row[0] for row in rows if row[0] is not None}


def read_layout(
    connection: psycopg.Connection,
    *,
    sensor_type: str | None = PHASE_SENSOR_TYPE,
    object_id: str | None = None,
) -> dict[str, LayoutRow]:
    clauses, params = [], []
    if sensor_type is not None:
        clauses.append("sensor_type = %s")
        params.append(sensor_type)
    if object_id is not None:
        clauses.append("object_id = %s")
        params.append(object_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = _q(connection).execute(
        f"""SELECT channel_id, object_id, sensor_type, system_type, name, tag, role, feeder_kind,
                   landmark_kind, picket_form, picket_from, picket_to, picket_basis
            FROM forecast_channel_layout {where}""",
        params,
    )
    return {row[0]: LayoutRow(*row) for row in rows}


def read_object_names(connection: psycopg.Connection) -> dict[str, str | None]:
    return dict(_q(connection).execute("SELECT object_id, object_name FROM forecast_objects"))


# -- detector -----------------------------------------------------------------------------------


def read_states(connection: psycopg.Connection, scope: Scope) -> dict[str, dict]:
    rows = _q(connection).execute(
        """SELECT channel_id, state FROM forecast_detector_state
           WHERE namespace_id = %s AND snapshot_id = %s""",
        scope.key,
    )
    return {channel: state for channel, state in rows}


def write_states(
    connection: psycopg.Connection, scope: Scope, states: dict[str, ChannelState]
) -> None:
    with connection.cursor() as cursor:
        cursor.executemany(
            """INSERT INTO forecast_detector_state
                 (namespace_id, snapshot_id, channel_id, object_id, first_seen, last_ts,
                  state, detector_version)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (namespace_id, snapshot_id, channel_id) DO UPDATE SET
                 object_id = EXCLUDED.object_id, first_seen = EXCLUDED.first_seen,
                 last_ts = EXCLUDED.last_ts, state = EXCLUDED.state,
                 detector_version = EXCLUDED.detector_version""",
            [
                (
                    *scope.key,
                    channel_id,
                    state.object_id,
                    state.first_seen,
                    state.last_ts,
                    Jsonb(state.to_json()),
                    DETECTOR_VERSION,
                )
                for channel_id, state in states.items()
            ],
        )


def upsert_episodes(connection: psycopg.Connection, scope: Scope, episodes: Iterable[Episode]):
    with connection.cursor() as cursor:
        cursor.executemany(
            """INSERT INTO forecast_phase_episodes
                 (namespace_id, snapshot_id, channel_id, start_at, object_id, end_at,
                  start_alarm, power_off_at, detector_version)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (namespace_id, snapshot_id, channel_id, start_at) DO UPDATE SET
                 object_id = EXCLUDED.object_id, end_at = EXCLUDED.end_at,
                 start_alarm = EXCLUDED.start_alarm, power_off_at = EXCLUDED.power_off_at,
                 detector_version = EXCLUDED.detector_version""",
            [
                (
                    *scope.key,
                    episode.channel_id,
                    episode.start_at,
                    episode.object_id,
                    episode.end_at,
                    episode.start_alarm,
                    episode.power_off_at,
                    DETECTOR_VERSION,
                )
                for episode in episodes
            ],
        )


def copy_episodes(connection: psycopg.Connection, scope: Scope, episodes: Iterable[Episode]):
    """Bulk insert of new episodes (the first detection of a scope)."""
    with connection.cursor() as cursor:
        with cursor.copy(
            """COPY forecast_phase_episodes (namespace_id, snapshot_id, channel_id, start_at,
                 object_id, end_at, start_alarm, power_off_at, detector_version) FROM STDIN"""
        ) as copy:
            for e in episodes:
                copy.write_row(
                    (
                        *scope.key,
                        e.channel_id,
                        e.start_at,
                        e.object_id,
                        e.end_at,
                        e.start_alarm,
                        e.power_off_at,
                        DETECTOR_VERSION,
                    )
                )


def insert_candidates(
    connection: psycopg.Connection, scope: Scope, candidates: Iterable[tuple[str, datetime]]
) -> None:
    rows = list(candidates)
    if not rows:
        return
    _q(connection).execute(
        """CREATE TEMP TABLE IF NOT EXISTS forecast_candidates_stage
             (channel_id text, candidate_at timestamptz) ON COMMIT DROP"""
    )
    _q(connection).execute("TRUNCATE forecast_candidates_stage")
    with connection.cursor() as cursor:
        with cursor.copy(
            "COPY forecast_candidates_stage (channel_id, candidate_at) FROM STDIN"
        ) as copy:
            for row in rows:
                copy.write_row(row)
    _q(connection).execute(
        """INSERT INTO forecast_phase_candidates (namespace_id, snapshot_id, channel_id,
             candidate_at)
           SELECT DISTINCT %s, %s, channel_id, candidate_at FROM forecast_candidates_stage
           ON CONFLICT DO NOTHING""",
        scope.key,
    )


def replace_channel_history(
    connection: psycopg.Connection,
    scope: Scope,
    channel_id: str,
    episodes: list[Episode],
    candidates: Iterable[tuple[str, datetime]],
) -> None:
    for table in ("forecast_phase_episodes", "forecast_phase_candidates"):
        _q(connection).execute(
            f"DELETE FROM {table} WHERE namespace_id = %s AND snapshot_id = %s AND channel_id = %s",
            (*scope.key, channel_id),
        )
    upsert_episodes(connection, scope, episodes)
    insert_candidates(connection, scope, candidates)


def read_episodes(
    connection: psycopg.Connection, scope: Scope, *, objects: Iterable[str] | None = None
) -> list[Episode]:
    params: list = [*scope.key]
    clause = ""
    if objects is not None:
        clause = " AND object_id = ANY(%s)"
        params.append(list(objects))
    rows = _q(connection).execute(
        f"""SELECT channel_id, object_id, start_at, end_at, start_alarm, power_off_at
            FROM forecast_phase_episodes
            WHERE namespace_id = %s AND snapshot_id = %s{clause}""",
        params,
    )
    return [Episode(*row) for row in rows]


def read_candidates(connection: psycopg.Connection, scope: Scope) -> list[tuple[str, datetime]]:
    return list(
        _q(connection).execute(
            """SELECT channel_id, candidate_at FROM forecast_phase_candidates
               WHERE namespace_id = %s AND snapshot_id = %s""",
            scope.key,
        )
    )


def replace_events(
    connection: psycopg.Connection, scope: Scope, objects: set[str], events: list[PhaseEvent]
) -> None:
    _q(connection).execute(
        """DELETE FROM forecast_phase_events
           WHERE namespace_id = %s AND snapshot_id = %s AND object_id = ANY(%s)""",
        (*scope.key, sorted(objects)),
    )
    with connection.cursor() as cursor:
        with cursor.copy(
            """COPY forecast_phase_events (namespace_id, snapshot_id, event_id, object_id,
                 start_at, last_start_at, size, channel_ids, episode_starts) FROM STDIN"""
        ) as copy:
            for event in events:
                copy.write_row(
                    (
                        *scope.key,
                        event.event_id,
                        event.object_id,
                        event.start_at,
                        event.last_start_at,
                        event.size,
                        json.dumps(list(event.channel_ids)),
                        json.dumps([start.isoformat() for start in event.episode_starts]),
                    )
                )


def read_events(
    connection: psycopg.Connection,
    scope: Scope,
    *,
    object_id: str | None = None,
    until: datetime | None = None,
) -> list[PhaseEvent]:
    params: list = [*scope.key]
    clause = ""
    if object_id is not None:
        clause += " AND object_id = %s"
        params.append(object_id)
    if until is not None:
        clause += " AND start_at <= %s"
        params.append(until)
    rows = _q(connection).execute(
        f"""SELECT event_id, object_id, start_at, last_start_at, channel_ids, episode_starts
            FROM forecast_phase_events WHERE namespace_id = %s AND snapshot_id = %s{clause}
            ORDER BY start_at, event_id""",
        params,
    )
    return [
        PhaseEvent(
            event_id=event_id,
            object_id=object_id,
            start_at=start,
            last_start_at=last,
            channel_ids=tuple(channels),
            episode_starts=tuple(datetime.fromisoformat(value) for value in starts),
        )
        for event_id, object_id, start, last, channels, starts in rows
    ]


# -- coverage -------------------------------------------------------------------------------------


def upsert_coverage(
    connection: psycopg.Connection,
    scope: Scope,
    days: Iterable[tuple[date, bool, bool, str]],
) -> None:
    """``(day, covered, policy, source)``; a covered day stays covered, policy stays policy."""
    with connection.cursor() as cursor:
        cursor.executemany(
            """INSERT INTO forecast_coverage_days
                 (namespace_id, snapshot_id, day, covered, policy, source)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (namespace_id, snapshot_id, day) DO UPDATE SET
                 covered = forecast_coverage_days.covered OR EXCLUDED.covered,
                 policy = forecast_coverage_days.policy OR EXCLUDED.policy""",
            [(*scope.key, *row) for row in days],
        )


def read_coverage(connection: psycopg.Connection, scope: Scope) -> dict[date, tuple[bool, bool]]:
    rows = _q(connection).execute(
        """SELECT day, covered, policy FROM forecast_coverage_days
           WHERE namespace_id = %s AND snapshot_id = %s""",
        scope.key,
    )
    return {day: (covered, policy) for day, covered, policy in rows}


def no_data_days_before(
    connection: psycopg.Connection, scope: Scope, cutoff: datetime
) -> tuple[date, date] | None:
    """Days (MSK) without usable data right before ``cutoff``: covered and not a policy day,
    as the scorer checks the day before each cutoff. Every cutoff issued on these days has
    no cards. None when the day before ``cutoff`` is usable or no earlier day is."""
    last = cutoff.astimezone(MSK).date() - timedelta(days=1)
    usable = (
        _q(connection)
        .execute(
            """SELECT max(day) FROM forecast_coverage_days
               WHERE namespace_id = %s AND snapshot_id = %s AND day <= %s
                 AND covered AND NOT policy""",
            (*scope.key, last),
        )
        .fetchone()[0]
    )
    if usable is None or usable >= last:
        return None
    return usable + timedelta(days=1), last


def aggregate_new_rows(
    connection: psycopg.Connection, scope: Scope, *, after_position: int | None
) -> None:
    """Coverage days (any sensor) and per-object phase days of the rows after a position.

    ``after_position`` None reads the whole scope (an immutable replay snapshot).
    """
    position = "" if after_position is None else " AND received_position > %s"
    params = (*scope.key, *(() if after_position is None else (after_position,)))
    _q(connection).execute(
        f"""INSERT INTO forecast_coverage_days
              (namespace_id, snapshot_id, day, covered, policy, source)
            SELECT %s, %s, day, true, false, 'observations'
            FROM (SELECT DISTINCT (event_at AT TIME ZONE 'Europe/Moscow')::date AS day
                  FROM dispatch_observations
                  WHERE namespace_id = %s AND snapshot_id = %s{position}) AS days
            ON CONFLICT (namespace_id, snapshot_id, day) DO UPDATE SET covered = true""",
        (*scope.key, *params),
    )
    _q(connection).execute(
        f"""INSERT INTO forecast_object_days
              (namespace_id, snapshot_id, object_id, day, last_record_at, records)
            SELECT %s, %s, object_id, (event_at AT TIME ZONE 'Europe/Moscow')::date,
                   max(event_at), count(*)
            FROM dispatch_observations
            WHERE namespace_id = %s AND snapshot_id = %s{position}
              AND sensor_type = %s AND object_id IS NOT NULL
            GROUP BY 3, 4
            ON CONFLICT (namespace_id, snapshot_id, object_id, day) DO UPDATE SET
              last_record_at = GREATEST(forecast_object_days.last_record_at,
                                        EXCLUDED.last_record_at),
              records = forecast_object_days.records + EXCLUDED.records""",
        (*scope.key, *params, PHASE_SENSOR_TYPE),
    )


def read_new_phase_rows(
    connection: psycopg.Connection, scope: Scope, *, after_position: int | None
) -> tuple[list[tuple], int]:
    """Phase records after a position ordered by channel and time, and the new watermark."""
    position = "" if after_position is None else " AND received_position > %s"
    params = (*scope.key, *(() if after_position is None else (after_position,)))
    top = (
        _q(connection)
        .execute(
            f"""SELECT max(received_position) FROM dispatch_observations
                WHERE namespace_id = %s AND snapshot_id = %s{position}""",
            params,
        )
        .fetchone()[0]
    )
    rows = (
        _q(connection)
        .execute(
            f"""SELECT channel_id, event_at, value_raw, alarm, object_id
                FROM dispatch_observations
                WHERE namespace_id = %s AND snapshot_id = %s{position} AND sensor_type = %s
                ORDER BY channel_id, event_at""",
            (*params, PHASE_SENSOR_TYPE),
        )
        .fetchall()
    )
    return rows, top if top is not None else (after_position or 0)


def read_channel_rows(connection: psycopg.Connection, scope: Scope, channel_id: str) -> list:
    return (
        _q(connection)
        .execute(
            """SELECT channel_id, event_at, value_raw, alarm, object_id
               FROM dispatch_observations
               WHERE namespace_id = %s AND snapshot_id = %s AND channel_id = %s
               ORDER BY event_at""",
            (*scope.key, channel_id),
        )
        .fetchall()
    )


def read_object_days(
    connection: psycopg.Connection, scope: Scope
) -> dict[str, list[tuple[date, datetime]]]:
    rows = _q(connection).execute(
        """SELECT object_id, day, last_record_at FROM forecast_object_days
           WHERE namespace_id = %s AND snapshot_id = %s ORDER BY object_id, day""",
        scope.key,
    )
    out: dict[str, list[tuple[date, datetime]]] = {}
    for object_id, day, last in rows:
        out.setdefault(object_id, []).append((day, last))
    return out

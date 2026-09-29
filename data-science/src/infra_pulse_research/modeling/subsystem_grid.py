"""Shared registries and the daily cutoff grid for ``subsystem-alarm-v1``.

The unit is ``object_id × system_type × issued_at`` at local midnight. A group
enters the grid only after its first source message strictly before
``issued_at``; nothing after the cutoff decides candidacy. Channel attributes
come from the current reference joined in notebook 08 and have no historical
validity dates. Rows without ``object_id`` or ``system_type`` are out of scope
and counted separately.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import duckdb

CONFIG_VERSION = "subsystem-alarm-v1"
GRID_FIRST_MONTH = "2019-01"
GRID_LAST_MONTH = "2026-06"


def _literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _identifier(value: str) -> str:
    if not value.replace("_", "").isalnum() or value[0].isdigit():
        raise ValueError(f"invalid SQL identifier: {value}")
    return '"' + value + '"'


def month_bounds(month: str) -> tuple[dt.date, dt.date]:
    start = dt.date.fromisoformat(month + "-01")
    if start.strftime("%Y-%m") != month:
        raise ValueError("month must be YYYY-MM")
    end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return start, end


def study_months(first: str = GRID_FIRST_MONTH, last: str = GRID_LAST_MONTH) -> list[str]:
    """All calendar months of the grid; 2021 rows are later excluded by label."""
    current, stop = month_bounds(first)[0], month_bounds(last)[1]
    months = []
    while current < stop:
        months.append(current.strftime("%Y-%m"))
        current = month_bounds(current.strftime("%Y-%m"))[1]
    return months


def materialize_registries(
    con: duckdb.DuckDBPyConnection,
    directory: Path,
    *,
    events_view: str = "current_enriched_events",
) -> dict:
    """Persist channel/group registries once and register them as views.

    ``first_seen`` is the first parseable source timestamp of the channel. The
    registry is keyed by channel; a channel with several object/system values
    would make group membership ambiguous and fails loudly.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    channels = directory / "channel_registry.parquet"
    out_of_scope = directory / "out_of_scope.json"
    view = _identifier(events_view)
    con.execute(f"""
        COPY (
          SELECT channel_id,
                 MIN(object_id) AS object_id,
                 MIN(system_type) AS system_type,
                 MIN(sensor_type) AS sensor_type,
                 COUNT(DISTINCT object_id) AS object_values,
                 COUNT(DISTINCT system_type) AS system_values,
                 COUNT(DISTINCT sensor_type) AS sensor_values,
                 MIN(TRY_CAST(event_ts_local_raw AS TIMESTAMP)) AS first_seen,
                 ANY_VALUE(sensor_name_current) AS sensor_name_current,
                 ANY_VALUE(object_kind) AS object_kind,
                 ANY_VALUE(picket_from) AS picket_from,
                 ANY_VALUE(picket_to) AS picket_to,
                 ANY_VALUE(picket_form) AS picket_form,
                 COALESCE(ANY_VALUE(picket_parsed), false) AS picket_parsed
          FROM {view}
          WHERE channel_id IS NOT NULL
            AND object_id IS NOT NULL
            AND system_type IS NOT NULL
            AND NOT COALESCE(is_epoch_placeholder, false)
            AND TRY_CAST(event_ts_local_raw AS TIMESTAMP) IS NOT NULL
          GROUP BY channel_id
          ORDER BY channel_id
        ) TO {_literal(channels)} (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    ambiguous = con.execute(
        f"SELECT COUNT(*) FROM read_parquet({_literal(channels)}) "
        "WHERE object_values > 1 OR system_values > 1 OR sensor_values > 1"
    ).fetchone()[0]
    if ambiguous:
        raise ValueError(f"{ambiguous} channels map to several groups or sensor types")
    scope = con.execute(f"""
        SELECT COUNT(*) AS rows, COUNT_IF(alarm) AS alarm_rows,
               COUNT(DISTINCT channel_id) AS channels
        FROM {view}
        WHERE object_id IS NULL OR system_type IS NULL
    """).fetchone()
    scope_record = {
        "rule": "rows without object_id or system_type have no group and are not candidates",
        "rows": scope[0],
        "alarm_rows": scope[1],
        "channels": scope[2],
    }
    out_of_scope.write_text(json.dumps(scope_record, indent=2) + "\n", encoding="utf-8")
    register_registry_views(con, directory)
    with channels.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    rows = con.execute("SELECT COUNT(*) FROM subsystem_channel_registry").fetchone()[0]
    groups = con.execute("SELECT COUNT(*) FROM subsystem_group_registry").fetchone()[0]
    return {
        "channel_registry_sha256": digest,
        "channels": rows,
        "groups": groups,
        "out_of_scope": scope_record,
    }


def register_registry_views(con: duckdb.DuckDBPyConnection, directory: Path) -> None:
    """Expose ``subsystem_channel_registry`` and ``subsystem_group_registry``."""
    channels = Path(directory) / "channel_registry.parquet"
    con.execute(f"""
        CREATE OR REPLACE VIEW subsystem_channel_registry AS
        SELECT * EXCLUDE (object_values, system_values, sensor_values)
        FROM read_parquet({_literal(channels)})
    """)
    con.execute("""
        CREATE OR REPLACE VIEW subsystem_group_registry AS
        SELECT object_id, system_type, MIN(first_seen) AS first_seen,
               ANY_VALUE(object_kind) AS object_kind
        FROM subsystem_channel_registry
        GROUP BY object_id, system_type
    """)


def register_cutoffs(
    con: duckdb.DuckDBPyConnection,
    month: str,
    *,
    view_name: str = "subsystem_cutoffs",
) -> None:
    """Daily local-midnight group cutoffs of ``month`` known strictly before t."""
    start, end = month_bounds(month)
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW {_identifier(view_name)} AS
        SELECT g.object_id, g.system_type, CAST(d.day AS TIMESTAMP) AS issued_at,
               {_literal(month)} AS month
        FROM subsystem_group_registry g
        CROSS JOIN generate_series(DATE {_literal(start)},
                                   DATE {_literal(end - dt.timedelta(days=1))},
                                   INTERVAL 1 DAY) d(day)
        WHERE g.first_seen < CAST(d.day AS TIMESTAMP)
    """)

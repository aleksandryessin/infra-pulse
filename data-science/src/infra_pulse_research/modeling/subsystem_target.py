"""Group-level registered-alarm labels for ``subsystem-alarm-v1``.

Unit: ``object_id × system_type × issued_at`` (naive local timestamps, the
Europe/Moscow assumption of the curated manifest). Candidacy and known channels
come only from the registry (``first_seen < issued_at``); the future defines the
label only. Silence is not unknown: a fully covered window without
``alarm=true`` rows is negative. ``alarm=true`` is a registered source signal,
never a confirmed fault.

The journal is scanned once by :func:`materialize_target_tables`; monthly
labeling then reads only the compact alarm-timestamp and episode tables.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import duckdb

from infra_pulse_research.modeling.subsystem_grid import (
    CONFIG_VERSION,
    _identifier,
    _literal,
    month_bounds,
)

CONFIG_PATH = Path(__file__).resolve().parents[3] / "configs" / "subsystem_alarm_v1.json"
TARGET_VERSION = "subsystem-alarm-v1-target-1"
EXCLUDED_SPLIT = "excluded_2021_and_lookback"
BEFORE_SPLITS = "insufficient_lookback"
AFTER_SPLITS = "after_data_end"
NEW_CHANNEL_DAYS = 7
PAST_ALARM_HOURS = 24
EPISODE_GAP_HOURS = 24
# Evaluated in this order; the first matching reason is stored.
EXCLUSION_PRECEDENCE = (
    "insufficient_lookback",
    "excluded_2021_and_lookback",
    "data_end",
    "split_boundary",
    "source_coverage",
    "policy_day",
)
LABEL_COLUMNS = (
    "object_id",
    "system_type",
    "system_code",
    "issued_at",
    "month",
    "split_period",
    "outcome",
    "y",
    "exclusion_reason",
    "known_channels",
    "known_sensor_types",
    "uncovered_window_days",
    "policy_window_days",
    "future_alarm_rows",
    "future_alarm_timestamps",
    "future_alarm_channels",
    "future_conflict_timestamps",
    "first_alarm_at",
    "lead_hours",
    "past24h_alarm",
    "stratum",
    "new_channel_alarm",
    "episode_id",
    "episode_start_in_window",
    "episode_start_at",
    "target_version",
)


def load_config(path: Path = CONFIG_PATH) -> dict:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if config["version"] != CONFIG_VERSION:
        raise ValueError("unexpected subsystem config version")
    if EXCLUDED_SPLIT not in config["splits"]:
        raise ValueError(f"config splits lack {EXCLUDED_SPLIT}")
    return config


def _split_intervals(config: dict) -> list[tuple[str, dt.date, dt.date]]:
    intervals = [
        (name, dt.date.fromisoformat(start), dt.date.fromisoformat(end))
        for name, ranges in config["splits"].items()
        for start, end in ranges
    ]
    intervals.sort(key=lambda item: item[1])
    for (_, _, end), (name, start, _) in zip(intervals, intervals[1:], strict=False):
        if start != end:
            raise ValueError(f"config splits are not contiguous before {name}")
    return intervals


def data_end(config: dict | None = None) -> dt.date:
    return _split_intervals(config or load_config())[-1][2]


def split_period_sql(expr: str, config: dict | None = None) -> str:
    """SQL CASE mapping a timestamp expression to the configured split name."""
    intervals = _split_intervals(config or load_config())
    cases = " ".join(
        f"WHEN {expr} >= TIMESTAMP {_literal(start)} AND {expr} < TIMESTAMP {_literal(end)} "
        f"THEN {_literal(name)}"
        for name, start, end in intervals
    )
    return (
        f"CASE {cases} WHEN {expr} < TIMESTAMP {_literal(intervals[0][1])} "
        f"THEN {_literal(BEFORE_SPLITS)} ELSE {_literal(AFTER_SPLITS)} END"
    )


def split_end_sql(expr: str, config: dict | None = None) -> str:
    """SQL CASE giving the end of the split interval that contains ``expr``."""
    intervals = _split_intervals(config or load_config())
    cases = " ".join(
        f"WHEN {expr} >= TIMESTAMP {_literal(start)} AND {expr} < TIMESTAMP {_literal(end)} "
        f"THEN TIMESTAMP {_literal(end)}"
        for _, start, end in intervals
    )
    return f"CASE {cases} ELSE NULL END"


def _system_code_sql(expr: str, config: dict) -> str:
    cases = " ".join(
        f"WHEN {expr} = {_literal(name)} THEN {_literal(code)}"
        for name, code in config["system_types"].items()
    )
    return f"CASE {cases} ELSE 'other' END"


def _digest(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def materialize_target_tables(
    con: duckdb.DuckDBPyConnection,
    directory: Path,
    *,
    events_view: str = "current_enriched_events",
) -> dict[str, Path]:
    """Scan the journal once; persist alarm timestamps and group episodes.

    ``alarm_timestamps`` keeps channel × timestamp keys with at least one
    ``alarm=true`` row plus the number of simultaneous ``alarm=false`` rows
    (conflicts). Episodes are group-level: a new episode starts when the gap to
    the previous alarm timestamp of the group exceeds 24 hours. Returns the
    written parquet files; counts and checksums go to ``manifest.json``.
    """
    config = load_config()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    view = _identifier(events_view)
    files = {
        "alarm_timestamps": directory / "alarm_timestamps.parquet",
        "episodes": directory / "episodes.parquet",
    }
    manifest_path = directory / "manifest.json"
    valid = """channel_id IS NOT NULL AND object_id IS NOT NULL AND system_type IS NOT NULL
               AND NOT COALESCE(is_epoch_placeholder, false)
               AND TRY_CAST(event_ts_local_raw AS TIMESTAMP) IS NOT NULL"""
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _subsystem_true_keys AS
        SELECT channel_id, TRY_CAST(event_ts_local_raw AS TIMESTAMP) AS event_at,
               MIN(object_id) AS object_id, MIN(system_type) AS system_type,
               MIN(sensor_type) AS sensor_type, COUNT(*) AS true_rows
        FROM {view}
        WHERE alarm AND {valid}
        GROUP BY channel_id, event_at
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _subsystem_false_keys AS
        SELECT k.channel_id, k.event_at, COUNT(*) AS false_rows
        FROM (
          SELECT channel_id, TRY_CAST(event_ts_local_raw AS TIMESTAMP) AS event_at
          FROM {view}
          WHERE alarm = false AND {valid}
            AND channel_id IN (SELECT DISTINCT channel_id FROM _subsystem_true_keys)
        ) k
        SEMI JOIN _subsystem_true_keys t
          ON t.channel_id = k.channel_id AND t.event_at = k.event_at
        GROUP BY k.channel_id, k.event_at
    """)
    gap = f"INTERVAL {EPISODE_GAP_HOURS} HOUR"
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _subsystem_alarm_ts AS
        WITH keyed AS (
          SELECT t.object_id, t.system_type, t.channel_id, t.sensor_type, t.event_at,
                 t.true_rows, COALESCE(f.false_rows, 0) AS false_rows
          FROM _subsystem_true_keys t
          LEFT JOIN _subsystem_false_keys f USING (channel_id, event_at)
        ), group_ts AS (
          SELECT object_id, system_type, event_at,
                 LAG(event_at) OVER (PARTITION BY object_id, system_type
                                     ORDER BY event_at) AS previous_group_alarm_at
          FROM (SELECT DISTINCT object_id, system_type, event_at FROM keyed)
        ), numbered AS (
          SELECT *,
                 SUM(CASE WHEN previous_group_alarm_at IS NULL
                           OR event_at - previous_group_alarm_at > {gap}
                          THEN 1 ELSE 0 END)
                   OVER (PARTITION BY object_id, system_type ORDER BY event_at
                         ROWS UNBOUNDED PRECEDING) AS episode_seq
          FROM group_ts
        ), starts AS (
          SELECT object_id, system_type, episode_seq, MIN(event_at) AS episode_start
          FROM numbered GROUP BY ALL
        )
        SELECT k.*,
               LAG(k.event_at) OVER (PARTITION BY k.channel_id ORDER BY k.event_at)
                 AS previous_channel_alarm_at,
               {_system_code_sql("k.system_type", config)} || ':' ||
                 CAST(k.object_id AS VARCHAR) || ':' ||
                 strftime(s.episode_start, '%Y-%m-%dT%H:%M:%S') AS episode_id
        FROM keyed k
        JOIN numbered n USING (object_id, system_type, event_at)
        JOIN starts s USING (object_id, system_type, episode_seq)
    """)
    con.execute(f"""
        COPY (SELECT * FROM _subsystem_alarm_ts
              ORDER BY event_at, object_id, system_type, channel_id)
        TO {_literal(files["alarm_timestamps"])} (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    con.execute(f"""
        COPY (
          SELECT episode_id, object_id, system_type,
                 MIN(event_at) AS episode_start, MAX(event_at) AS episode_end,
                 SUM(true_rows) AS alarm_rows,
                 COUNT(DISTINCT channel_id) AS alarm_channels,
                 COUNT(DISTINCT event_at) AS alarm_timestamps,
                 COUNT_IF(false_rows > 0) AS conflict_timestamps
          FROM _subsystem_alarm_ts
          GROUP BY episode_id, object_id, system_type
          ORDER BY episode_start, object_id, system_type
        ) TO {_literal(files["episodes"])} (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    dropped = con.execute(f"""
        SELECT COUNT_IF(channel_id IS NULL) AS no_channel,
               COUNT_IF(channel_id IS NOT NULL AND (object_id IS NULL OR system_type IS NULL))
                 AS no_group,
               COUNT_IF(COALESCE(is_epoch_placeholder, false)) AS epoch_placeholder,
               COUNT_IF(TRY_CAST(event_ts_local_raw AS TIMESTAMP) IS NULL) AS unparsed_ts,
               COUNT(*) AS true_rows_total
        FROM {view} WHERE alarm
    """).fetchone()
    stats = con.execute("""
        SELECT COUNT(*), SUM(true_rows), COUNT_IF(false_rows > 0),
               COUNT(DISTINCT channel_id), COUNT(DISTINCT episode_id)
        FROM _subsystem_alarm_ts
    """).fetchone()
    for table in ("_subsystem_true_keys", "_subsystem_false_keys", "_subsystem_alarm_ts"):
        con.execute(f"DROP TABLE IF EXISTS {table}")
    manifest = {
        "target_version": TARGET_VERSION,
        "config_version": config["version"],
        "events_view": events_view,
        "episode_gap_hours": EPISODE_GAP_HOURS,
        "alarm_timestamps": {
            "file": files["alarm_timestamps"].name,
            "sha256": _digest(files["alarm_timestamps"]),
            "channel_timestamps": stats[0],
            "true_rows": int(stats[1] or 0),
            "conflict_timestamps": stats[2],
            "channels": stats[3],
        },
        "episodes": {
            "file": files["episodes"].name,
            "sha256": _digest(files["episodes"]),
            "episodes": stats[4],
        },
        "true_rows_in_source": dropped[4],
        "true_rows_not_used": {
            "no_channel": dropped[0],
            "no_object_or_system": dropped[1],
            "epoch_placeholder": dropped[2],
            "unparsed_timestamp": dropped[3],
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    register_target_tables(con, directory)
    return files


def register_target_tables(con: duckdb.DuckDBPyConnection, directory: Path) -> None:
    """Expose materialized files as ``subsystem_alarm_timestamps`` and
    ``subsystem_alarm_episodes`` without rescanning the journal."""
    directory = Path(directory)
    con.execute(f"""
        CREATE OR REPLACE VIEW subsystem_alarm_timestamps AS
        SELECT * FROM read_parquet({_literal(directory / "alarm_timestamps.parquet")})
    """)
    con.execute(f"""
        CREATE OR REPLACE VIEW subsystem_alarm_episodes AS
        SELECT * FROM read_parquet({_literal(directory / "episodes.parquet")})
    """)


def label_sql(month: str, *, cutoffs_view: str = "subsystem_cutoffs") -> str:
    """Label query for cutoffs of ``month``; needs registry, target, coverage, policy views."""
    config = load_config()
    start, end = month_bounds(month)
    horizon = f"INTERVAL {int(config['horizon_hours'])} HOUR"
    past = f"INTERVAL {PAST_ALARM_HOURS} HOUR"
    new_channel = f"INTERVAL {NEW_CHANNEL_DAYS} DAY"
    lower = start - dt.timedelta(days=NEW_CHANNEL_DAYS + 2)
    upper = end + dt.timedelta(days=2)
    end_of_data = data_end(config)
    return f"""
    WITH cut AS (
      SELECT object_id, system_type, issued_at,
             CAST(issued_at AS DATE) AS d0,
             CAST(issued_at + {horizon} - INTERVAL 1 MICROSECOND AS DATE) AS d1,
             issued_at + {horizon} AS window_end,
             {split_period_sql("issued_at", config)} AS split_period,
             {split_end_sql("issued_at", config)} AS split_end
      FROM {_identifier(cutoffs_view)}
    ), alarms AS (
      SELECT * FROM subsystem_alarm_timestamps
      WHERE event_at >= TIMESTAMP {_literal(lower)} AND event_at < TIMESTAMP {_literal(upper)}
    ), known AS (
      SELECT c.object_id, c.system_type, c.issued_at, COUNT(*) AS known_channels,
             list_sort(LIST(DISTINCT r.sensor_type)) AS known_sensor_types
      FROM cut c JOIN subsystem_channel_registry r
        ON r.object_id = c.object_id AND r.system_type = c.system_type
       AND r.first_seen < c.issued_at
      GROUP BY ALL
    ), windows AS (
      SELECT DISTINCT d0, d1 FROM cut
    ), cover AS (
      SELECT w.d0, w.d1,
             date_diff('day', w.d0, w.d1) + 1
               - COUNT(DISTINCT v.day) FILTER (WHERE v.working_covered)
               AS uncovered_window_days
      FROM windows w LEFT JOIN coverage_days v ON v.day >= w.d0 AND v.day <= w.d1
      GROUP BY w.d0, w.d1
    ), policy AS (
      SELECT c.object_id, c.system_type, c.issued_at,
             COUNT(DISTINCT p.day) AS policy_window_days
      FROM cut c
      JOIN known k USING (object_id, system_type, issued_at)
      JOIN policy_days p ON p.day >= c.d0 AND p.day <= c.d1
      WHERE p.sensor_type_scope IS NULL
         OR list_contains(k.known_sensor_types, p.sensor_type_scope)
      GROUP BY ALL
    ), fut AS (
      SELECT c.object_id, c.system_type, c.issued_at,
             SUM(a.true_rows) AS future_alarm_rows,
             COUNT(*) AS future_alarm_timestamps,
             COUNT(DISTINCT a.channel_id) AS future_alarm_channels,
             COUNT_IF(a.false_rows > 0) AS future_conflict_timestamps,
             MIN(a.event_at) AS first_alarm_at,
             BOOL_OR(a.previous_channel_alarm_at IS NULL OR
                     a.previous_channel_alarm_at < c.issued_at - {new_channel})
               AS any_new_channel
      FROM cut c JOIN alarms a
        ON a.object_id = c.object_id AND a.system_type = c.system_type
       AND a.event_at >= c.issued_at AND a.event_at < c.window_end
      GROUP BY ALL
    ), past AS (
      SELECT c.object_id, c.system_type, c.issued_at, COUNT(*) AS past_alarm_timestamps
      FROM cut c JOIN alarms a
        ON a.object_id = c.object_id AND a.system_type = c.system_type
       AND a.event_at >= c.issued_at - {past} AND a.event_at < c.issued_at
      GROUP BY ALL
    ), ep AS (
      SELECT c.object_id, c.system_type, c.issued_at,
             arg_min(e.episode_id, e.episode_start) AS episode_id,
             MIN(e.episode_start) AS episode_start_at
      FROM cut c JOIN subsystem_alarm_episodes e
        ON e.object_id = c.object_id AND e.system_type = c.system_type
       AND e.episode_start >= c.issued_at AND e.episode_start < c.window_end
      GROUP BY ALL
    ), joined AS (
      SELECT c.*, COALESCE(k.known_channels, 0) AS known_channels, k.known_sensor_types,
             COALESCE(v.uncovered_window_days, 0) AS uncovered_window_days,
             COALESCE(p.policy_window_days, 0) AS policy_window_days,
             COALESCE(f.future_alarm_rows, 0) AS future_alarm_rows,
             COALESCE(f.future_alarm_timestamps, 0) AS future_alarm_timestamps,
             COALESCE(f.future_alarm_channels, 0) AS future_alarm_channels,
             COALESCE(f.future_conflict_timestamps, 0) AS future_conflict_timestamps,
             f.first_alarm_at, COALESCE(f.any_new_channel, false) AS any_new_channel,
             COALESCE(s.past_alarm_timestamps, 0) > 0 AS past24h_alarm,
             e.episode_id, e.episode_start_at,
             CASE
               WHEN c.split_period = {_literal(BEFORE_SPLITS)} THEN 'insufficient_lookback'
               WHEN c.split_period = {_literal(EXCLUDED_SPLIT)} THEN {_literal(EXCLUDED_SPLIT)}
               WHEN c.split_period = {_literal(AFTER_SPLITS)}
                 OR c.window_end > TIMESTAMP {_literal(end_of_data)} THEN 'data_end'
               WHEN c.window_end > c.split_end THEN 'split_boundary'
               WHEN COALESCE(v.uncovered_window_days, 0) > 0 THEN 'source_coverage'
               WHEN COALESCE(p.policy_window_days, 0) > 0 THEN 'policy_day'
             END AS exclusion_reason
      FROM cut c
      LEFT JOIN known k USING (object_id, system_type, issued_at)
      LEFT JOIN cover v ON v.d0 = c.d0 AND v.d1 = c.d1
      LEFT JOIN policy p USING (object_id, system_type, issued_at)
      LEFT JOIN fut f USING (object_id, system_type, issued_at)
      LEFT JOIN past s USING (object_id, system_type, issued_at)
      LEFT JOIN ep e USING (object_id, system_type, issued_at)
    ), labeled AS (
      SELECT *,
             CASE WHEN exclusion_reason IS NOT NULL THEN 'excluded'
                  WHEN future_alarm_timestamps > 0 THEN 'positive'
                  ELSE 'negative' END AS outcome
      FROM joined
    )
    SELECT object_id, system_type, {_system_code_sql("system_type", config)} AS system_code,
           issued_at, {_literal(month)} AS month, split_period, outcome,
           CASE outcome WHEN 'positive' THEN 1 WHEN 'negative' THEN 0 END::INTEGER AS y,
           exclusion_reason, known_channels, known_sensor_types,
           uncovered_window_days, policy_window_days,
           future_alarm_rows, future_alarm_timestamps, future_alarm_channels,
           future_conflict_timestamps, first_alarm_at,
           date_diff('second', issued_at, first_alarm_at) / 3600.0 AS lead_hours,
           past24h_alarm,
           CASE WHEN outcome = 'positive' AND past24h_alarm THEN 'continuation'
                WHEN outcome = 'positive' THEN 'onset_after_24h_quiet' END AS stratum,
           outcome = 'positive' AND any_new_channel AS new_channel_alarm,
           episode_id, episode_id IS NOT NULL AS episode_start_in_window,
           episode_start_at, {_literal(TARGET_VERSION)} AS target_version
    FROM labeled
    WHERE issued_at >= TIMESTAMP {_literal(start)} AND issued_at < TIMESTAMP {_literal(end)}
    ORDER BY issued_at, object_id, system_type
    """


def label_month(
    con: duckdb.DuckDBPyConnection,
    month: str,
    output_dir: Path,
    *,
    cutoffs_view: str = "subsystem_cutoffs",
) -> dict:
    """Write ``month=YYYY-MM/labels.parquet`` and its manifest; return the manifest."""
    start, end = month_bounds(month)
    view = _identifier(cutoffs_view)
    total, distinct, outside, unknown = con.execute(f"""
        SELECT COUNT(*), COUNT(DISTINCT (object_id, system_type, issued_at)),
               COUNT_IF(issued_at < TIMESTAMP {_literal(start)}
                        OR issued_at >= TIMESTAMP {_literal(end)}),
               COUNT_IF(NOT EXISTS (
                 SELECT 1 FROM subsystem_channel_registry r
                 WHERE r.object_id = c.object_id AND r.system_type = c.system_type
                   AND r.first_seen < c.issued_at))
        FROM {view} c
    """).fetchone()
    if total != distinct:
        raise ValueError("cutoffs are not unique per object_id × system_type × issued_at")
    if outside:
        raise ValueError(f"{outside} cutoffs lie outside month {month}")
    if unknown:
        raise ValueError(f"{unknown} cutoffs have no channel known before issued_at")
    directory = Path(output_dir) / f"month={month}"
    directory.mkdir(parents=True, exist_ok=True)
    labels = directory / "labels.parquet"
    con.execute(
        f"COPY ({label_sql(month, cutoffs_view=cutoffs_view)}) "
        f"TO {_literal(labels)} (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    rows = con.execute(f"SELECT COUNT(*) FROM read_parquet({_literal(labels)})").fetchone()[0]
    if rows != total:
        raise ValueError(f"label rows {rows} differ from cutoffs {total}")
    summary = con.execute(f"""
        SELECT outcome, exclusion_reason, COUNT(*) AS n
        FROM read_parquet({_literal(labels)}) GROUP BY ALL ORDER BY ALL
    """).fetchall()
    outcomes = {name: 0 for name in ("positive", "negative", "excluded")}
    reasons: dict[str, int] = {}
    for outcome, reason, n in summary:
        outcomes[outcome] += n
        if reason is not None:
            reasons[reason] = reasons.get(reason, 0) + n
    evaluable = outcomes["positive"] + outcomes["negative"]
    manifest = {
        "month": month,
        "target_version": TARGET_VERSION,
        "config_version": CONFIG_VERSION,
        "file": labels.name,
        "rows": rows,
        "sha256": _digest(labels),
        "outcomes": outcomes,
        "exclusion_reasons": reasons,
        "positive_rate": outcomes["positive"] / evaluable if evaluable else None,
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest

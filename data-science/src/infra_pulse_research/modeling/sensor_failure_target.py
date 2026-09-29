"""Channel-level registered technical events for ``sensor-failure-v1``.

Labels: ``technical_value`` (manufacturer technical values or out-of-range
readings of numeric channels) and ``connection_loss`` (configured text states;
"Неисправен" means only a lost device connection). Both are registered events,
not confirmed breakdowns.

Per channel and label a timestamp is a *candidate* when any row matches the
label, *clean* when it has only clean rows (see config), otherwise neutral.
Mixed candidate/clean timestamps are candidates with a conflict; records are
never ordered inside one timestamp. An episode starts on a candidate with no
candidate in the previous Q hours and at least one clean timestamp since the
previous candidate; it ends at the first clean timestamp after the start.
Episode starts of one object and label chain into mass clusters when
consecutive starts are at most W minutes apart.

Rows flagged ``is_epoch_placeholder`` are kept when their timestamp parses: the
epoch is the value of "Состояние охраны", not the event time. ``alarm`` is read
per channel × timestamp (numeric rows never carry it; the paired text row does).

Episode starts depend only on records up to the start, so ``active_episode``,
``hours_since_last_candidate`` and ``candidate_past_7d`` at a cutoff are
past-only; future records define only the label.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import duckdb

from infra_pulse_research.modeling.subsystem_grid import _identifier, _literal, month_bounds
from infra_pulse_research.modeling.subsystem_target import (
    data_end,
    split_end_sql,
    split_period_sql,
)

CONFIG_PATH = Path(__file__).resolve().parents[3] / "configs" / "sensor_failure_v1.json"
CONFIG_VERSION = "sensor-failure-v1"
TARGET_VERSION = "sensor-failure-v1-labels-1"
EXCLUDED_SPLIT = "excluded"  # split name; exclusion reason is excluded_2021_and_lookback
PAST_CANDIDATE_DAYS = 7


def load_config(path: Path = CONFIG_PATH) -> dict:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if config["version"] != CONFIG_VERSION:
        raise ValueError("unexpected sensor-failure config version")
    for hours in config["horizons_hours"]:
        if hours % 24:
            raise ValueError("horizons must be whole days")
    return config


def provisional_technical_sql(alias: str) -> str:
    """Fallback technical-reason SQL used only if ``sensor_technical_values`` is absent."""
    return (
        f"(CASE WHEN {alias}.sensor_type = 'Датчик температуры' THEN CASE "
        f"WHEN {alias}.value_numeric = -127 THEN 'code' "
        f"WHEN {alias}.value_numeric < -40 THEN 'below_range' "
        f"WHEN {alias}.value_numeric > 85 THEN 'above_range' END END)"
    )


def _value_sql(alias: str) -> str:
    return (
        f"COALESCE({alias}.value_numeric, "
        f"TRY_CAST(replace(trim({alias}.value_raw), ',', '.') AS DOUBLE))"
    )


def resolve_technical_sql() -> tuple[Callable[[str], str], str]:
    """Technical-reason SQL provider: returns a reason string or NULL per row."""
    try:
        from infra_pulse_research.modeling import sensor_technical_values  # noqa: PLC0415
    except ImportError:
        return provisional_technical_sql, "provisional_fallback"
    version = sensor_technical_values.load_config()["version"]
    return sensor_technical_values.technical_reason_sql, f"technical_reason_sql:{version}"


def _in_list(values) -> str:
    return ", ".join(_literal(v) for v in values)


def label_predicates(
    alias: str, config: dict, technical_sql: Callable[[str], str]
) -> dict[str, dict[str, str]]:
    """Candidate/clean SQL predicates and scope filter per label.

    ``technical_sql(alias)`` returns a technical reason or NULL per row; the
    label keeps reasons not listed in ``exclude_reasons``. Text labels are a
    list of rules (states × sensor types). Clean text rows are in scope, have
    ``value_raw`` and are neither a state of any rule nor neutral.
    """
    result = {}
    for name, spec in config["labels"].items():
        if spec["kind"] == "technical_value":
            scope = spec.get("sensor_types")
            scope_sql = f"{alias}.sensor_type IN ({_in_list(scope)})" if scope else "true"
            reason = f"({technical_sql(alias)})"
            excluded = spec.get("exclude_reasons", [])
            keep = f" AND {reason} NOT IN ({_in_list(excluded)})" if excluded else ""
            candidate = f"COALESCE({scope_sql} AND {reason} IS NOT NULL{keep}, false)"
            any_value = spec.get("clean_any_value_sensor_types", [])
            valued = f"{_value_sql(alias)} IS NOT NULL"
            if any_value:
                valued = (
                    f"({valued} OR ({alias}.sensor_type IN ({_in_list(any_value)}) "
                    f"AND {alias}.value_raw IS NOT NULL))"
                )
            clean = f"COALESCE({scope_sql} AND {reason} IS NULL AND {valued}, false)"
        elif spec["kind"] == "text_state":
            rules, scopes, states = [], [], []
            for rule in spec["rules"]:
                types = rule.get("sensor_types")
                rule_scope = f"{alias}.sensor_type IN ({_in_list(types)})" if types else "true"
                scopes.append(rule_scope)
                states += rule["states"]
                rules.append(
                    f"({rule_scope} AND {alias}.value_raw IN ({_in_list(rule['states'])}))"
                )
            scope_sql = "(" + " OR ".join(scopes) + ")"
            candidate = f"COALESCE({' OR '.join(rules)}, false)"
            silent = sorted({*states, *spec.get("neutral_states", [])})
            clean = (
                f"COALESCE({scope_sql} AND {alias}.value_raw IS NOT NULL "
                f"AND {alias}.value_raw NOT IN ({_in_list(silent)}), false)"
            )
        else:
            raise ValueError(f"unknown label kind: {spec['kind']}")
        result[name] = {"candidate": candidate, "clean": clean, "scope": scope_sql}
    return result


def _digest(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _events_sql(view: str, predicates: dict) -> str:
    flags = ",\n".join(
        f"{p['candidate']} AS cand_{i}, {p['clean']} AS clean_{i}"
        for i, p in enumerate(predicates.values())
    )
    return f"""
        SELECT e.channel_id, TRY_CAST(e.event_ts_local_raw AS TIMESTAMP) AS event_at,
               CAST(TRY_CAST(e.event_ts_local_raw AS TIMESTAMP) AS DATE) AS event_day,
               COALESCE(e.alarm, false) AS alarm, {flags}
        FROM {_identifier(view)} e
        WHERE e.channel_id IS NOT NULL
          AND TRY_CAST(e.event_ts_local_raw AS TIMESTAMP) IS NOT NULL
    """


def _first_clean_after(
    con: duckdb.DuckDBPyConnection, events: str, labels: list[str], spans: list
) -> None:
    """Fill ``_sf_first_clean`` for ``_sf_anchors`` by bounded journal passes.

    Each pass reads only (channel, day) pairs within ``span`` days after the
    unresolved anchors (``None`` = rest of history for the pending channels).
    """
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _sf_first_clean AS
        SELECT label, channel_id, anchor_at, NULL::TIMESTAMP AS first_clean_at
        FROM _sf_anchors WHERE false
    """)
    per_label = ", ".join(
        f"BOOL_OR(cand_{i}) AS cand_{i}, BOOL_OR(clean_{i}) AS clean_{i}"
        for i in range(len(labels))
    )
    for span in spans:
        con.execute("""
            CREATE OR REPLACE TEMP TABLE _sf_pending AS
            SELECT a.* FROM _sf_anchors a
            ANTI JOIN _sf_first_clean f USING (label, channel_id, anchor_at)
        """)
        if not con.execute("SELECT COUNT(*) FROM _sf_pending").fetchone()[0]:
            break
        if span is None:
            con.execute("""
                CREATE OR REPLACE TEMP TABLE _sf_scope AS
                SELECT channel_id, MIN(anchor_at) AS since FROM _sf_pending GROUP BY channel_id
            """)
            restrict = (
                "SEMI JOIN _sf_scope s ON s.channel_id = e.channel_id AND e.event_at > s.since"
            )
        else:
            con.execute(f"""
                CREATE OR REPLACE TEMP TABLE _sf_scope AS
                SELECT DISTINCT channel_id, CAST(d.day AS DATE) AS day
                FROM _sf_pending,
                     generate_series(CAST(anchor_at AS DATE),
                                     CAST(anchor_at AS DATE) + INTERVAL {int(span) - 1} DAY,
                                     INTERVAL 1 DAY) d(day)
            """)
            restrict = (
                "SEMI JOIN _sf_scope s ON s.channel_id = e.channel_id AND s.day = e.event_day"
            )
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE _sf_window AS
            SELECT e.channel_id, e.event_at, {per_label}
            FROM ({events}) e {restrict}
            GROUP BY e.channel_id, e.event_at
        """)
        # The window is the union of all pending anchors' spans; each anchor may
        # only use its own span, or a later anchor's clean record would end an
        # earlier episode too late (audit 26.09, LEAKAGE_AUDIT.md F1).
        own_span = (
            ""
            if span is None
            else "AND w.event_at < CAST(CAST(p.anchor_at AS DATE) AS TIMESTAMP)"
            f" + INTERVAL {int(span)} DAY"
        )
        for i, label in enumerate(labels):
            con.execute(f"""
                INSERT INTO _sf_first_clean
                SELECT p.label, p.channel_id, p.anchor_at, MIN(w.event_at)
                FROM _sf_pending p JOIN _sf_window w
                  ON w.channel_id = p.channel_id AND w.event_at > p.anchor_at
                 AND w.clean_{i} AND NOT w.cand_{i} {own_span}
                WHERE p.label = {_literal(label)}
                GROUP BY p.label, p.channel_id, p.anchor_at
            """)
    con.execute("""
        INSERT INTO _sf_first_clean
        SELECT label, channel_id, anchor_at, NULL FROM _sf_anchors
        ANTI JOIN _sf_first_clean USING (label, channel_id, anchor_at)
    """)


def materialize_failure_tables(
    con: duckdb.DuckDBPyConnection,
    directory: Path,
    *,
    events_view: str = "current_enriched_events",
    config: dict | None = None,
    technical_sql: Callable[[str], str] | None = None,
) -> dict[str, Path]:
    """Scan the journal a few times; persist candidates, episodes and clusters.

    Needs ``subsystem_channel_registry``, ``coverage_days`` and ``policy_days``.
    Writes one episode file per Q and one cluster file per (Q, W) of the config
    grids; episode files carry ``cluster_id_w{W}``/``cluster_size_w{W}``.
    """
    config = config or load_config()
    provider = "injected"
    if technical_sql is None:
        technical_sql, provider = resolve_technical_sql()
    predicates = label_predicates("e", config, technical_sql)
    labels = list(predicates)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    events = _events_sql(events_view, predicates)
    any_candidate = " OR ".join(f"cand_{i}" for i in range(len(labels)))
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _sf_raw AS
        SELECT * FROM ({events}) WHERE {any_candidate}
    """)
    union = " UNION ALL ".join(
        f"SELECT {_literal(label)} AS label, channel_id, event_at, COUNT(*) AS candidate_rows, "
        f"COUNT_IF(alarm) AS candidate_alarm_rows FROM _sf_raw WHERE cand_{i} "
        "GROUP BY channel_id, event_at"
        for i, label in enumerate(labels)
    )
    con.execute(f"CREATE OR REPLACE TEMP TABLE _sf_cand_raw AS {union}")
    clean_counts = ", ".join(f"COUNT_IF(clean_{i}) AS clean_{i}" for i in range(len(labels)))
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _sf_same_ts AS
        SELECT e.channel_id, e.event_at, COUNT(*) AS rows_at_ts,
               COUNT_IF(e.alarm) AS alarm_rows_at_ts, {clean_counts}
        FROM ({events}) e
        SEMI JOIN (SELECT DISTINCT channel_id, event_at FROM _sf_cand_raw) k
          ON k.channel_id = e.channel_id AND k.event_at = e.event_at
        GROUP BY e.channel_id, e.event_at
    """)
    clean_case = " ".join(
        f"WHEN {_literal(label)} THEN s.clean_{i}" for i, label in enumerate(labels)
    )
    files = {"candidates": directory / "candidates.parquet"}
    con.execute(f"""
        COPY (
          SELECT c.label, c.channel_id, r.object_id, r.system_type, r.sensor_type, c.event_at,
                 c.candidate_rows, c.candidate_alarm_rows, s.rows_at_ts, s.alarm_rows_at_ts,
                 CASE c.label {clean_case} END AS clean_rows_at_ts,
                 LAG(c.event_at) OVER (PARTITION BY c.label, c.channel_id ORDER BY c.event_at)
                   AS previous_candidate_at
          FROM _sf_cand_raw c
          JOIN subsystem_channel_registry r USING (channel_id)
          JOIN _sf_same_ts s USING (channel_id, event_at)
          ORDER BY c.label, c.event_at, c.channel_id
        ) TO {_literal(files["candidates"])} (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    unmatched = con.execute("""
        SELECT label, COUNT(*) FROM _sf_cand_raw
        ANTI JOIN subsystem_channel_registry USING (channel_id) GROUP BY label ORDER BY label
    """).fetchall()
    cand = f"read_parquet({_literal(files['candidates'])})"
    q_grid = [int(q) for q in config["episode"]["q_hours_grid"]]
    potential = " UNION ".join(
        f"SELECT label, channel_id, previous_candidate_at AS anchor_at FROM {cand} "
        f"WHERE previous_candidate_at IS NOT NULL AND event_at - previous_candidate_at "
        f">= INTERVAL {q} HOUR UNION SELECT label, channel_id, event_at FROM {cand} "
        f"WHERE previous_candidate_at IS NULL OR event_at - previous_candidate_at "
        f">= INTERVAL {q} HOUR"
        for q in q_grid
    )
    con.execute(f"CREATE OR REPLACE TEMP TABLE _sf_anchors AS {potential}")
    _first_clean_after(con, events, labels, list(config["episode"]["end_search_days"]))
    stats = {}
    for q in q_grid:
        path = directory / f"episodes_q{q}h.parquet"
        files[f"episodes_q{q}h"] = path
        _write_episodes(con, cand, q, config, path)
        stats[f"q{q}h"] = con.execute(f"""
            SELECT label, COUNT(*) AS episodes, COUNT_IF(end_at IS NULL) AS open_episodes
            FROM read_parquet({_literal(path)}) GROUP BY label ORDER BY label
        """).fetchall()
        stuck = con.execute(f"""
            SELECT c.label, COUNT(*) FROM {cand} c
            JOIN _sf_first_clean f ON f.label = c.label AND f.channel_id = c.channel_id
             AND f.anchor_at = c.previous_candidate_at
            WHERE c.event_at - c.previous_candidate_at >= INTERVAL {q} HOUR
              AND (f.first_clean_at IS NULL OR f.first_clean_at >= c.event_at)
            GROUP BY c.label ORDER BY c.label
        """).fetchall()
        stats[f"q{q}h_gap_without_recovery"] = stuck
        for w in config["cluster"]["w_minutes_grid"]:
            cpath = directory / f"clusters_q{q}h_w{int(w)}m.parquet"
            files[f"clusters_q{q}h_w{int(w)}m"] = cpath
            con.execute(f"""
                COPY (
                  SELECT cluster_id, label, object_id, MIN(start_at) AS cluster_start,
                         MAX(start_at) AS cluster_last_start, COUNT(*) AS size,
                         COUNT(DISTINCT sensor_type) AS sensor_types,
                         COUNT(DISTINCT system_type) AS system_types,
                         date_diff('second', MIN(start_at), MAX(start_at)) / 60.0 AS span_minutes
                  FROM (SELECT label, object_id, sensor_type, system_type, start_at,
                               cluster_id_w{int(w)} AS cluster_id
                        FROM read_parquet({_literal(path)}))
                  GROUP BY cluster_id, label, object_id ORDER BY cluster_start, cluster_id
                ) TO {_literal(cpath)} (FORMAT PARQUET, COMPRESSION ZSTD)
            """)
    for table in (
        "_sf_raw",
        "_sf_cand_raw",
        "_sf_same_ts",
        "_sf_anchors",
        "_sf_first_clean",
        "_sf_pending",
        "_sf_scope",
        "_sf_window",
    ):
        con.execute(f"DROP TABLE IF EXISTS {table}")
    manifest = {
        "target_version": TARGET_VERSION,
        "config_version": config["version"],
        "events_view": events_view,
        "technical_value_provider": provider,
        "labels": config["labels"],
        "q_hours_grid": q_grid,
        "w_minutes_grid": config["cluster"]["w_minutes_grid"],
        "candidate_timestamps_without_registry_channel": dict(unmatched),
        "episodes": stats,
        "files": {name: {"file": p.name, "sha256": _digest(p)} for name, p in files.items()},
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return files


def _write_episodes(con, cand: str, q: int, config: dict, path: Path) -> None:
    windows = [int(w) for w in config["cluster"]["w_minutes_grid"]]
    cluster_cols = []
    cluster_ctes = []
    for w in windows:
        cluster_ctes.append(f"""
        , chain_w{w} AS (
          SELECT episode_id, SUM(CASE WHEN previous_start IS NULL OR
                                   start_at - previous_start > INTERVAL {w} MINUTE
                                  THEN 1 ELSE 0 END)
                   OVER (PARTITION BY label, object_id ORDER BY start_at, channel_id
                         ROWS UNBOUNDED PRECEDING) AS seq, label, object_id, start_at
          FROM (SELECT *, LAG(start_at) OVER (PARTITION BY label, object_id
                                              ORDER BY start_at, channel_id) AS previous_start
                FROM starts)
        ), cluster_w{w} AS (
          SELECT episode_id,
                 label || ':' || CAST(object_id AS VARCHAR) || ':' ||
                   strftime(MIN(start_at) OVER (PARTITION BY label, object_id, seq),
                            '%Y-%m-%dT%H:%M:%S') || ':w{w}' AS cluster_id_w{w},
                 COUNT(*) OVER (PARTITION BY label, object_id, seq) AS cluster_size_w{w}
          FROM chain_w{w}
        )""")
        cluster_cols.append(f"cluster_id_w{w}, cluster_size_w{w}")
    joins = " ".join(f"JOIN cluster_w{w} USING (episode_id)" for w in windows)
    con.execute(f"""
        COPY (
          WITH cand AS (SELECT * FROM {cand}),
          potential AS (
            SELECT c.*, f.first_clean_at AS clean_after_previous
            FROM cand c
            LEFT JOIN _sf_first_clean f ON f.label = c.label AND f.channel_id = c.channel_id
             AND f.anchor_at = c.previous_candidate_at
            WHERE c.previous_candidate_at IS NULL
               OR c.event_at - c.previous_candidate_at >= INTERVAL {q} HOUR
          ), starts0 AS (
            SELECT p.label, p.channel_id, p.object_id, p.system_type, p.sensor_type,
                   p.event_at AS start_at, p.previous_candidate_at,
                   p.candidate_rows AS start_candidate_rows,
                   p.candidate_alarm_rows > 0 AS start_candidate_alarm,
                   p.alarm_rows_at_ts > 0 AS start_alarm_at_ts,
                   p.clean_rows_at_ts > 0 AS start_conflict,
                   f.first_clean_at AS end_at
            FROM potential p
            LEFT JOIN _sf_first_clean f ON f.label = p.label AND f.channel_id = p.channel_id
             AND f.anchor_at = p.event_at
            WHERE p.previous_candidate_at IS NULL OR p.clean_after_previous < p.event_at
          ), starts AS (
            SELECT *, label || ':' || CAST(channel_id AS VARCHAR) || ':' ||
                      strftime(start_at, '%Y-%m-%dT%H:%M:%S') AS episode_id,
                      LEAD(start_at) OVER (PARTITION BY label, channel_id ORDER BY start_at)
                        AS next_start_at
            FROM starts0
          ), members AS (
            SELECT s.episode_id, COUNT(*) AS candidate_timestamps,
                   MAX(c.event_at) AS last_candidate_at
            FROM starts s JOIN cand c ON c.label = s.label AND c.channel_id = s.channel_id
             AND c.event_at >= s.start_at
             AND (s.next_start_at IS NULL OR c.event_at < s.next_start_at)
            GROUP BY s.episode_id
          ), lookback AS (
            SELECT s.episode_id,
                   COUNT(*) FILTER (WHERE NOT COALESCE(v.working_covered, false))
                     AS uncovered_lookback_days,
                   COUNT(*) FILTER (WHERE EXISTS (
                     SELECT 1 FROM policy_days p WHERE p.day = CAST(d.day AS DATE)
                       AND (p.sensor_type_scope IS NULL OR p.sensor_type_scope = s.sensor_type)))
                     AS policy_lookback_days
            FROM starts s,
                 generate_series(CAST(s.start_at - INTERVAL {q} HOUR AS DATE),
                                 CAST(s.start_at - INTERVAL 1 MICROSECOND AS DATE),
                                 INTERVAL 1 DAY) d(day)
            LEFT JOIN coverage_days v ON v.day = CAST(d.day AS DATE)
            GROUP BY s.episode_id
          ) {"".join(cluster_ctes)}
          SELECT s.episode_id, s.label, s.channel_id, s.object_id, s.system_type, s.sensor_type,
                 {q} AS q_hours, s.start_at, s.end_at,
                 date_diff('second', s.start_at, s.end_at) / 3600.0 AS duration_hours,
                 s.end_at IS NULL AS open_at_data_end, s.previous_candidate_at,
                 s.next_start_at, m.candidate_timestamps, m.last_candidate_at,
                 s.start_candidate_rows, s.start_candidate_alarm, s.start_alarm_at_ts,
                 s.start_conflict,
                 r.first_seen <= s.start_at - INTERVAL {q} HOUR
                   AND l.uncovered_lookback_days = 0 AND l.policy_lookback_days = 0
                   AS q_history_ok,
                 l.uncovered_lookback_days, l.policy_lookback_days,
                 {", ".join(cluster_cols)}
          FROM starts s
          JOIN members m USING (episode_id)
          JOIN lookback l USING (episode_id)
          JOIN subsystem_channel_registry r ON r.channel_id = s.channel_id
          {joins}
          ORDER BY s.label, s.start_at, s.channel_id
        ) TO {_literal(path)} (FORMAT PARQUET, COMPRESSION ZSTD)
    """)


def register_failure_tables(
    con: duckdb.DuckDBPyConnection, directory: Path, *, q_hours: int, w_minutes: int
) -> None:
    """Expose ``sensor_failure_candidates``, ``sensor_failure_episodes`` (with the
    ``cluster_id``/``cluster_size`` of W) and ``sensor_failure_clusters``."""
    directory = Path(directory)
    con.execute(f"""
        CREATE OR REPLACE VIEW sensor_failure_candidates AS
        SELECT * FROM read_parquet({_literal(directory / "candidates.parquet")})
    """)
    con.execute(f"""
        CREATE OR REPLACE VIEW sensor_failure_episodes AS
        SELECT *, cluster_id_w{int(w_minutes)} AS cluster_id,
               cluster_size_w{int(w_minutes)} AS cluster_size, {int(w_minutes)} AS w_minutes
        FROM read_parquet({_literal(directory / f"episodes_q{int(q_hours)}h.parquet")})
    """)
    con.execute(f"""
        CREATE OR REPLACE VIEW sensor_failure_clusters AS
        SELECT * FROM read_parquet(
          {_literal(directory / f"clusters_q{int(q_hours)}h_w{int(w_minutes)}m.parquet")})
    """)


def _tag(hours: float) -> str:
    return f"{hours:g}".replace(".", "p")


WINDOW_REASONS = (
    "not_weekly_cutoff",
    "insufficient_lookback",
    "excluded_2021_and_lookback",
    "data_end",
    "split_boundary",
    "source_coverage",
    "policy_day",
    "lookback_coverage",
)
PROTOCOLS = ("measurement", "model")


def load_model_config(
    config: dict | None = None, model_config: Path | str | dict | None = None
) -> dict:
    """Frozen model protocol (v1 by default); its periods must equal the label splits.

    ``model_config`` may be a path (absolute or relative to ``data-science/``)
    or an already loaded dict, e.g. ``configs/sensor_failure_model_v2.json``.
    """
    config = config or load_config()
    if isinstance(model_config, dict):
        model = model_config
    else:
        path = Path(model_config or config["model_config"])
        if not path.is_absolute():
            path = CONFIG_PATH.parents[1] / path
        model = json.loads(path.read_text(encoding="utf-8"))
    periods = {k: v for k, v in model["periods"].items() if isinstance(v, list)}
    splits = {k: v for k, v in config["splits"].items() if k in periods}
    if periods != splits:
        raise ValueError("label config splits differ from the model periods")
    return model


def _qualifying(alias: str, rule) -> str:
    """Episode filter: None (all), minimum hours (v1) or ``{"op", "seconds"}`` (v2).

    Open episodes (no end before data end) always qualify. Seconds are counted
    with ``date_diff('second', start_at, end_at)`` on 1 s source timestamps.
    """
    if rule is None:
        return "true"
    if isinstance(rule, dict):
        if rule["op"] not in (">", ">="):
            raise ValueError("episode filter op must be '>' or '>='")
        seconds = int(rule["seconds"])
        test = f"date_diff('second', {alias}.start_at, {alias}.end_at) {rule['op']} {seconds}"
        return f"({alias}.end_at IS NULL OR {test})"
    return f"({alias}.end_at IS NULL OR {alias}.duration_hours >= {float(rule)!r})"


def _split_sql(expr: str, config: dict) -> str:
    raw = split_period_sql(expr, config)
    outside = "('insufficient_lookback', 'after_data_end')"
    return f"(CASE WHEN ({raw}) IN {outside} THEN 'excluded' ELSE ({raw}) END)"


def check_cutoff_hour(cutoff_hour) -> int:
    """Daily issue time as a whole local hour in [0, 23]; 0 is the v1/v2 midnight."""
    hour = int(cutoff_hour)
    if hour != cutoff_hour or not 0 <= hour <= 23:
        raise ValueError("cutoff_hour must be a whole hour in [0, 23]")
    return hour


def _cutoff_windows(horizons: list[int], q_days: int, q_hours: int, cutoff_hour: int) -> dict:
    """Calendar-day predicates of the forward and lookback windows of ``c.day``.

    A cutoff ``t = day + cutoff_hour`` has the forward window ``[t, t + H)`` and the
    lookback ``[t − Q, t)``; a window covers every calendar day it touches. At
    ``cutoff_hour = 0`` these are the midnight predicates of v1/v2.
    """
    if not cutoff_hour:
        back_days, last = q_days, "<"
    else:
        back_days, last = -(-(int(q_hours) - cutoff_hour) // 24), "<="
    return {
        "fwd": {
            h: f"x.day >= c.day AND x.day {last} c.day + INTERVAL {h // 24} DAY" for h in horizons
        },
        "pol_fwd": {
            h: f"p.day >= c.day AND p.day {last} c.day + INTERVAL {h // 24} DAY" for h in horizons
        },
        "back_x": f"x.day >= c.day - INTERVAL {back_days} DAY AND x.day {last} c.day",
        "back_p": f"p.day {last} c.day AND p.day >= c.day - INTERVAL {back_days} DAY",
        "join_hi": last,
    }


def label_sql(
    month: str,
    *,
    label: str,
    q_hours: int,
    config: dict | None = None,
    protocol: str = "measurement",
    min_duration_hours=None,
    horizons: list[int] | None = None,
    weekly_horizons: list[int] | None = None,
    cutoff_hour: int = 0,
) -> str:
    """Channel-day labels of ``month`` for all configured horizons (wide).

    ``protocol='measurement'`` reproduces the label measurement (168 h daily, Q
    shadow only flagged, duration-variant columns). ``protocol='model'`` follows
    the frozen model protocol: window reasons first, channels in an active
    episode or the Q shadow are not candidates, ``weekly_horizons`` (v1: 168 h)
    only on Mondays, and only episodes passing ``min_duration_hours`` — hours or a
    ``{"op", "seconds"}`` filter — (or open) are positive. ``horizons`` defaults
    to the label config.

    ``cutoff_hour`` moves the daily issue time from local midnight (default, v1/v2)
    to ``day + cutoff_hour``: the label window is ``[t, t + H)``, eligibility, the
    active episode and the Q shadow are evaluated at ``t``, and coverage/policy days
    are all calendar days touched by the forward window or the lookback ``[t − Q, t)``.
    Starts between midnight and ``t`` are past (history), never label.
    """
    if protocol not in PROTOCOLS:
        raise ValueError(f"protocol must be one of {PROTOCOLS}")
    cutoff_hour = check_cutoff_hour(cutoff_hour)
    config = config or load_config()
    spec = config["labels"][label]
    start, end = month_bounds(month)
    horizons = [int(h) for h in (horizons or config["horizons_hours"])]
    if any(h % 24 for h in horizons):
        raise ValueError("horizons must be whole days")
    longest = max(horizons)
    q_days = -(-int(q_hours) // 24)
    scope_sql = label_predicates("r", {"labels": {label: spec}}, provisional_technical_sql)[label][
        "scope"
    ]
    model = protocol == "model"
    durations = (
        []
        if model
        else [float(d) for d in config["episode"].get("min_duration_hours_variants", [])]
    )
    qualifying = _qualifying("e", min_duration_hours if model else None)
    if not model:
        weekly = set()
    else:
        weekly = {168} if weekly_horizons is None else {int(h) for h in weekly_horizons}
    cal_lo = start - dt.timedelta(days=q_days + 1)
    cal_hi = end + dt.timedelta(days=longest // 24 + 1)
    win = _cutoff_windows(horizons, q_days, q_hours, cutoff_hour)
    issued = "CAST(d.day AS TIMESTAMP)"
    if cutoff_hour:
        issued += f" + INTERVAL {cutoff_hour} HOUR"
    fwd = ",\n".join(
        f"(SELECT COUNT(*) FROM cal x WHERE {win['fwd'][h]} AND NOT x.covered) AS unc_{h}"
        for h in horizons
    )
    pol_fwd = ",\n".join(
        f"COUNT(DISTINCT p.day) FILTER (WHERE {win['pol_fwd'][h]}) AS pol_{h}" for h in horizons
    )
    fut = ",\n".join(
        f"""MIN(e.start_at) FILTER (WHERE e.start_at < c.issued_at + INTERVAL {h} HOUR)
               AS first_start_{h}h,
             arg_min(e.cluster_id, e.start_at) FILTER (
               WHERE e.start_at < c.issued_at + INTERVAL {h} HOUR) AS cluster_id_{h}h,
             arg_min(e.cluster_size, e.start_at) FILTER (
               WHERE e.start_at < c.issued_at + INTERVAL {h} HOUR) AS cluster_size_{h}h,
             COUNT(e.start_at) FILTER (WHERE e.start_at < c.issued_at + INTERVAL {h} HOUR)
               AS starts_{h}h"""
        + "".join(
            f""", COUNT(e.start_at) FILTER (WHERE e.start_at < c.issued_at + INTERVAL {h} HOUR
                   AND (e.end_at IS NULL OR e.duration_hours >= {d}))
                 AS starts_{h}h_dur{_tag(d)}h"""
            for d in durations
        )
        for h in horizons
    )
    end_of_data = data_end(config)
    per_h = []
    for h in horizons:
        window_end = f"b.issued_at + INTERVAL {h} HOUR"
        window = [
            ("not_weekly_cutoff", f"{h in weekly} AND dayofweek(b.issued_at) <> 1"),
            ("insufficient_lookback", "b.raw_split = 'insufficient_lookback'"),
            ("excluded_2021_and_lookback", f"b.raw_split = {_literal(EXCLUDED_SPLIT)}"),
            (
                "data_end",
                f"b.raw_split = 'after_data_end' OR "
                f"{window_end} > TIMESTAMP {_literal(end_of_data)}",
            ),
            ("split_boundary", f"{window_end} > b.split_end"),
            ("source_coverage", f"b.unc_{h} > 0"),
            ("policy_day", f"COALESCE(b.pol_{h}, 0) > 0"),
            ("lookback_coverage", "b.unc_back > 0 OR COALESCE(b.pol_back, 0) > 0"),
        ]
        channel = [("no_prior_history", "NOT b.known"), ("active_episode", "b.active_episode")]
        if model:
            channel.append(("q_shadow", "b.in_q_shadow"))
            ordered = window + channel
        else:
            ordered = window[1:5] + channel[:1] + window[5:] + channel[1:]
        reason = "CASE " + " ".join(f"WHEN {c} THEN '{r}'" for r, c in ordered) + " END"
        window_reason = "CASE " + " ".join(f"WHEN {c} THEN '{r}'" for r, c in window) + " END"
        per_h.append(
            f"""
            {window_reason} AS window_reason_{h}h,
            {reason} AS exclusion_reason_{h}h,
            CASE WHEN ({reason}) IS NOT NULL THEN 'excluded'
                 WHEN f.first_start_{h}h IS NOT NULL THEN 'positive'
                 ELSE 'negative' END AS outcome_{h}h,
            CASE WHEN ({reason}) IS NOT NULL THEN NULL
                 WHEN f.first_start_{h}h IS NOT NULL THEN 1 ELSE 0 END::INTEGER AS y_{h}h,
            f.first_start_{h}h,
            date_diff('second', b.issued_at, f.first_start_{h}h) / 3600.0 AS lead_hours_{h}h,
            f.cluster_id_{h}h, f.cluster_size_{h}h, COALESCE(f.starts_{h}h, 0) AS starts_{h}h"""
            + "".join(
                f""",
            CASE WHEN ({reason}) IS NOT NULL THEN NULL
                 WHEN COALESCE(f.starts_{h}h_dur{_tag(d)}h, 0) > 0 THEN 1
                 ELSE 0 END::INTEGER AS y_{h}h_dur{_tag(d)}h"""
                for d in durations
            )
        )
    return f"""
    WITH cal AS (
      SELECT CAST(d.day AS DATE) AS day, COALESCE(v.working_covered, false) AS covered
      FROM generate_series(DATE {_literal(cal_lo)}, DATE {_literal(cal_hi)}, INTERVAL 1 DAY) d(day)
      LEFT JOIN coverage_days v ON v.day = CAST(d.day AS DATE)
    ), days AS (
      SELECT CAST(d.day AS DATE) AS day FROM generate_series(
        DATE {_literal(start)}, DATE {_literal(end - dt.timedelta(days=1))}, INTERVAL 1 DAY) d(day)
    ), day_cov AS (
      SELECT c.day, {fwd},
             (SELECT COUNT(*) FROM cal x WHERE {win["back_x"]} AND NOT x.covered) AS unc_back
      FROM days c
    ), cut AS (
      SELECT r.channel_id, r.object_id, r.system_type, r.sensor_type, r.first_seen,
             {issued} AS issued_at, d.day
      FROM subsystem_channel_registry r CROSS JOIN days d
      WHERE r.first_seen < TIMESTAMP {_literal(end)} AND {scope_sql}
    ), pol AS (
      SELECT c.channel_id, c.issued_at, {pol_fwd},
             COUNT(DISTINCT p.day) FILTER (WHERE {win["back_p"]}) AS pol_back
      FROM cut c JOIN policy_days p
        ON (p.sensor_type_scope IS NULL OR p.sensor_type_scope = c.sensor_type)
       AND p.day >= c.day - INTERVAL {q_days} DAY
       AND p.day {win["join_hi"]} c.day + INTERVAL {longest // 24} DAY
      GROUP BY c.channel_id, c.issued_at
    ), cands AS (
      SELECT channel_id, event_at FROM sensor_failure_candidates
      WHERE label = {_literal(label)} AND event_at < TIMESTAMP {_literal(end)}
    ), eps AS (
      SELECT channel_id, start_at, end_at FROM sensor_failure_episodes
      WHERE label = {_literal(label)} AND start_at < TIMESTAMP {_literal(end)}
    ), past_c AS (
      SELECT c.channel_id, c.issued_at, a.event_at AS last_candidate_at
      FROM cut c ASOF LEFT JOIN cands a
        ON a.channel_id = c.channel_id AND c.issued_at > a.event_at
    ), past_e AS (
      SELECT c.channel_id, c.issued_at, e.start_at AS last_start_at,
             CASE WHEN e.end_at < c.issued_at THEN e.end_at END AS last_end_at,
             e.start_at IS NOT NULL AND (e.end_at IS NULL OR e.end_at >= c.issued_at)
               AS active_episode
      FROM cut c ASOF LEFT JOIN eps e
        ON e.channel_id = c.channel_id AND c.issued_at > e.start_at
    ), f AS (
      SELECT c.channel_id, c.issued_at, {fut}
      FROM cut c JOIN sensor_failure_episodes e
        ON e.label = {_literal(label)} AND e.channel_id = c.channel_id
       AND e.start_at >= c.issued_at AND e.start_at < c.issued_at + INTERVAL {longest} HOUR
       AND {qualifying}
      GROUP BY c.channel_id, c.issued_at
    ), b AS (
      SELECT c.*, {split_period_sql("c.issued_at", config)} AS raw_split,
             {_split_sql("c.issued_at", config)} AS split_period,
             {split_end_sql("c.issued_at", config)} AS split_end,
             c.first_seen < c.issued_at AS known,
             dc.* EXCLUDE (day), p.* EXCLUDE (channel_id, issued_at),
             pc.last_candidate_at, pe.last_start_at, pe.last_end_at,
             COALESCE(pe.active_episode, false) AS active_episode,
             COALESCE(pc.last_candidate_at > c.issued_at - INTERVAL {int(q_hours)} HOUR, false)
               AS in_q_shadow
      FROM cut c
      JOIN day_cov dc ON dc.day = c.day
      LEFT JOIN pol p USING (channel_id, issued_at)
      LEFT JOIN past_c pc USING (channel_id, issued_at)
      LEFT JOIN past_e pe USING (channel_id, issued_at)
    )
    SELECT b.channel_id, b.object_id, b.system_type, b.sensor_type, {_literal(label)} AS label,
           {int(q_hours)} AS q_hours, b.issued_at, {_literal(month)} AS month, b.split_period,
           b.known, b.active_episode, b.in_q_shadow,
           b.known AND NOT b.active_episode AND NOT b.in_q_shadow AS candidate,
           b.last_start_at, b.last_end_at, b.last_candidate_at,
           date_diff('second', b.last_candidate_at, b.issued_at) / 3600.0
             AS hours_since_last_candidate,
           COALESCE(b.last_candidate_at >= b.issued_at - INTERVAL {PAST_CANDIDATE_DAYS} DAY, false)
             AS candidate_past_7d,
           {",".join(per_h)},
           {_literal(TARGET_VERSION)} AS target_version
    FROM b LEFT JOIN f USING (channel_id, issued_at)
    ORDER BY b.issued_at, b.channel_id
    """


def _outcome_counts(con, src: str, horizons) -> dict:
    result = {}
    for h in horizons:
        counts = con.execute(
            f"SELECT outcome_{h}h, exclusion_reason_{h}h, COUNT(*) FROM {src} "
            "GROUP BY ALL ORDER BY ALL"
        ).fetchall()
        outcomes = {"positive": 0, "negative": 0, "excluded": 0}
        reasons: dict[str, int] = {}
        for outcome, reason, n in counts:
            outcomes[outcome] += n
            if reason is not None:
                reasons[reason] = reasons.get(reason, 0) + n
        result[f"{h}h"] = {"outcomes": outcomes, "exclusion_reasons": reasons}
    return result


def _copy_checked(con, sql: str, path: Path, keys: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY ({sql}) TO {_literal(path)} (FORMAT PARQUET, COMPRESSION ZSTD)")
    rows, distinct = con.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT ({keys})) FROM read_parquet({_literal(path)})"
    ).fetchone()
    if rows != distinct:
        raise ValueError(f"{path.name}: rows are not unique per {keys}")
    return rows


def label_month(
    con: duckdb.DuckDBPyConnection,
    month: str,
    output_dir: Path,
    *,
    label: str,
    q_hours: int,
    w_minutes: int,
    config: dict | None = None,
) -> dict:
    """Measurement protocol: ``{label}_q{Q}h_w{W}m/month=YYYY-MM/labels.parquet``.

    The registered episode view must match ``q_hours``/``w_minutes``
    (see :func:`register_failure_tables`).
    """
    config = config or load_config()
    _check_registered(con, q_hours, w_minutes)
    directory = Path(output_dir) / f"{label}_q{int(q_hours)}h_w{int(w_minutes)}m" / f"month={month}"
    labels = directory / "labels.parquet"
    rows = _copy_checked(
        con,
        label_sql(month, label=label, q_hours=q_hours, config=config),
        labels,
        "channel_id, issued_at",
    )
    manifest = {
        "month": month,
        "label": label,
        "q_hours": int(q_hours),
        "w_minutes": int(w_minutes),
        "protocol": "measurement",
        "target_version": TARGET_VERSION,
        "config_version": config["version"],
        "file": labels.name,
        "rows": rows,
        "sha256": _digest(labels),
        "horizons": _outcome_counts(
            con, f"read_parquet({_literal(labels)})", config["horizons_hours"]
        ),
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _check_registered(con, q_hours: int, w_minutes: int) -> None:
    registered = con.execute(
        "SELECT DISTINCT q_hours, w_minutes FROM sensor_failure_episodes"
    ).fetchall()
    if registered and registered != [(int(q_hours), int(w_minutes))]:
        raise ValueError(f"registered episodes are {registered}, not Q={q_hours} W={w_minutes}")


def target_spec(target: str, config: dict | None = None, *, model_config=None) -> dict:
    """Target, episode filter, Q/W, horizons and weekly horizons of a model config.

    v1 targets live in the label config (``min_duration_hours``; the v1 model
    config only describes them in words); v2 targets in the model config
    (machine-readable ``episode_filter``). v1 issues 168 h weekly; a model config
    with ``weekly_horizons`` states it explicitly.
    """
    config = config or load_config()
    model = load_model_config(config, model_config)
    declared = model.get("targets", {}).get(target, {})
    if "label" in declared and not isinstance(declared.get("episode_filter"), str):
        spec = dict(declared)
        spec["filter"] = spec.get("episode_filter")
    else:
        spec = dict(config["targets"][target])
        spec["filter"] = spec.get("min_duration_hours")
    spec["model_version"] = model["version"]
    spec["q_hours"] = int(model["episode"]["q_hours"])
    spec["w_minutes"] = int(model["episode"]["cluster_window_minutes"])
    spec["horizons"] = [int(h.removesuffix("h")) for h in model["horizons"]]
    spec["weekly_horizons"] = [int(h) for h in model.get("weekly_horizons", [168])]
    spec["cutoff_hour"] = check_cutoff_hour(model.get("cutoff_hour", 0))
    return spec


def materialize_target_events(
    con: duckdb.DuckDBPyConnection,
    target: str,
    directory: Path,
    *,
    config: dict | None = None,
    model_config=None,
) -> dict[str, Path]:
    """Events (W-clusters of the label) with the target's qualifying flag.

    ``events.parquet``: one row per event of the label with ``qualifies``;
    ``event_members.parquet``: member episodes with their ``qualifying`` flag.
    Registers ``sf_events`` and ``sf_event_members`` for :func:`label_target_month`.
    """
    config = config or load_config()
    spec = target_spec(target, config, model_config=model_config)
    _check_registered(con, spec["q_hours"], spec["w_minutes"])
    base = Path(directory) / target
    files = {"events": base / "events.parquet", "event_members": base / "event_members.parquet"}
    qualifying = _qualifying("e", spec["filter"])
    members = f"""
        SELECT e.cluster_id AS event_id, e.episode_id, e.channel_id, e.object_id, e.sensor_type,
               e.system_type, e.start_at, e.end_at, e.duration_hours, {qualifying} AS qualifying
        FROM sensor_failure_episodes e WHERE e.label = {_literal(spec["label"])}
    """
    _copy_checked(
        con, members + " ORDER BY e.start_at, e.channel_id", files["event_members"], "episode_id"
    )
    _copy_checked(
        con,
        f"""
        SELECT event_id, {_literal(target)} AS target, {_literal(spec["label"])} AS label,
               object_id, MIN(start_at) AS event_start, MAX(start_at) AS event_last_start,
               COUNT(*) AS size, COUNT_IF(qualifying) AS qualifying_members,
               COUNT_IF(qualifying) > 0 AS qualifies, COUNT(*) >= 2 AS mass,
               list_sort(LIST(DISTINCT sensor_type)) AS sensor_types,
               list_sort(LIST(DISTINCT system_type)) AS system_types,
               MAX(duration_hours) AS max_duration_hours, BOOL_OR(end_at IS NULL) AS any_open,
               {_split_sql("MIN(start_at)", config)} AS split_period
        FROM ({members}) GROUP BY event_id, object_id ORDER BY event_start, event_id
        """,
        files["events"],
        "event_id",
    )
    register_target_events(con, directory, target)
    return files


def register_target_events(con: duckdb.DuckDBPyConnection, directory: Path, target: str) -> None:
    base = Path(directory) / target
    con.execute(
        "CREATE OR REPLACE VIEW sf_events AS SELECT * FROM "
        f"read_parquet({_literal(base / 'events.parquet')})"
    )
    con.execute(
        "CREATE OR REPLACE VIEW sf_event_members AS SELECT * FROM "
        f"read_parquet({_literal(base / 'event_members.parquet')})"
    )


def _cards_sql(
    channels: Path, target: str, label: str, h: int, month: str, weekly_horizons=(168,)
) -> tuple[str, str]:
    weekly = "WHERE dayofweek(issued_at) = 1" if h in set(weekly_horizons) else ""
    ch = f"(SELECT * FROM read_parquet({_literal(channels)}) {weekly})"
    links = f"""
        SELECT ch.object_id, ch.sensor_type, ch.issued_at, ev.event_id, ev.event_start,
               BOOL_OR(ch.candidate) AS candidate_member,
               COUNT(*) AS member_channels, COUNT_IF(ch.candidate) AS candidate_member_channels
        FROM {ch} ch
        JOIN sf_event_members m ON m.channel_id = ch.channel_id
        JOIN sf_events ev ON ev.event_id = m.event_id AND ev.qualifies
         AND ev.event_start >= ch.issued_at AND ev.event_start < ch.issued_at + INTERVAL {h} HOUR
        GROUP BY ch.object_id, ch.sensor_type, ch.issued_at, ev.event_id, ev.event_start
    """
    cards = f"""
        SELECT object_id, sensor_type, issued_at, MIN(system_type) AS system_type,
               COUNT(DISTINCT system_type) AS system_types, MIN(split_period) AS split_period,
               COUNT_IF(known) AS known_channels, COUNT_IF(candidate) AS candidate_channels,
               MIN(window_reason_{h}h) AS window_reason
        FROM {ch} GROUP BY object_id, sensor_type, issued_at HAVING COUNT_IF(known) > 0
    """
    card_sql = f"""
        WITH cards AS ({cards}), links AS ({links}),
        hit AS (
          SELECT object_id, sensor_type, issued_at, COUNT(*) AS events_in_window,
                 MIN(event_start) AS first_event_start
          FROM links WHERE candidate_member GROUP BY ALL
        ), fail AS (
          SELECT o.object_id, o.issued_at, COUNT(k.event_at) > 0 AS object_failing_past_24h
          FROM (SELECT DISTINCT object_id, issued_at FROM cards) o
          LEFT JOIN sensor_failure_candidates k ON k.label = {_literal(label)}
           AND k.object_id = o.object_id AND k.event_at >= o.issued_at - INTERVAL 24 HOUR
           AND k.event_at < o.issued_at
          GROUP BY o.object_id, o.issued_at
        ), r AS (
          SELECT c.*, COALESCE(c.window_reason,
                   CASE WHEN c.candidate_channels = 0 THEN 'no_candidate_channels' END)
                   AS exclusion_reason,
                 COALESCE(h.events_in_window, 0) AS events_in_window, h.first_event_start,
                 f.object_failing_past_24h
          FROM cards c LEFT JOIN hit h USING (object_id, sensor_type, issued_at)
          LEFT JOIN fail f USING (object_id, issued_at)
        )
        SELECT {_literal(target)} AS target, {h} AS horizon_hours, object_id, sensor_type,
               system_type, system_types, issued_at, {_literal(month)} AS month, split_period,
               known_channels, candidate_channels, exclusion_reason,
               CASE WHEN exclusion_reason IS NOT NULL THEN 'excluded'
                    WHEN events_in_window > 0 THEN 'positive' ELSE 'negative' END AS outcome,
               CASE WHEN exclusion_reason IS NOT NULL THEN NULL
                    WHEN events_in_window > 0 THEN 1 ELSE 0 END::INTEGER AS y_card,
               events_in_window, first_event_start,
               date_diff('second', issued_at, first_event_start) / 3600.0 AS lead_hours,
               object_failing_past_24h, dayofweek(issued_at) IN (0, 6) AS weekend
        FROM r ORDER BY issued_at, object_id, sensor_type
    """
    link_sql = f"""
        WITH cards AS ({cards}), links AS ({links})
        SELECT {_literal(target)} AS target, {h} AS horizon_hours, l.object_id, l.sensor_type,
               l.issued_at, c.split_period, l.event_id, l.event_start, l.candidate_member,
               l.member_channels, l.candidate_member_channels,
               c.object_id IS NOT NULL AS card_exists,
               COALESCE(c.window_reason,
                        CASE WHEN c.object_id IS NULL THEN 'no_card'
                             WHEN c.candidate_channels = 0 THEN 'no_candidate_channels' END)
                 AS card_exclusion_reason
        FROM links l LEFT JOIN cards c USING (object_id, sensor_type, issued_at)
        ORDER BY l.issued_at, l.event_id, l.sensor_type
    """
    return card_sql, link_sql


def label_target_month(
    con: duckdb.DuckDBPyConnection,
    month: str,
    output_dir: Path,
    *,
    target: str,
    config: dict | None = None,
    model_config=None,
    cutoff_hour: int | None = None,
) -> dict:
    """Model protocol: channel labels, cards and event↔card links of one month.

    Layout under ``output_dir`` (plain directories, not hive partitions):
    ``channels/T/M.parquet``, ``cards/T/Hh/M.parquet``, ``links/T/Hh/M.parquet``
    and ``manifests/T/M.json``. Requires registered failure tables
    (model Q/W) and :func:`materialize_target_events` for the target.
    ``model_config`` selects the protocol (default v1, or the v2 config path).
    ``cutoff_hour`` (default: the model config's, else 0 = midnight) sets the
    daily issue time; see :func:`label_sql`. Events do not depend on it.
    """
    config = config or load_config()
    spec = target_spec(target, config, model_config=model_config)
    hour = spec["cutoff_hour"] if cutoff_hour is None else check_cutoff_hour(cutoff_hour)
    _check_registered(con, spec["q_hours"], spec["w_minutes"])
    registered = con.execute("SELECT DISTINCT target FROM sf_events").fetchall()
    if registered and registered != [(target,)]:
        raise ValueError(f"registered events are for {registered}, not {target}")
    root = Path(output_dir)
    channels = root / f"channels/{target}/{month}.parquet"
    sql = label_sql(
        month,
        label=spec["label"],
        q_hours=spec["q_hours"],
        config=config,
        protocol="model",
        min_duration_hours=spec["filter"],
        horizons=spec["horizons"],
        weekly_horizons=spec["weekly_horizons"],
        cutoff_hour=hour,
    )
    rows = _copy_checked(con, sql, channels, "channel_id, issued_at")
    src = f"read_parquet({_literal(channels)})"
    manifest = {
        "month": month,
        "target": target,
        "label": spec["label"],
        "episode_filter": spec["filter"],
        "model_version": spec["model_version"],
        "q_hours": spec["q_hours"],
        "w_minutes": spec["w_minutes"],
        "protocol": "model",
        "target_version": TARGET_VERSION,
        "config_version": config["version"],
        # v1/v2 manifests (midnight) keep their exact keys.
        **({"cutoff_hour": hour} if hour else {}),
        "channels": {
            "rows": rows,
            "sha256": _digest(channels),
            "candidates": con.execute(f"SELECT COUNT_IF(candidate) FROM {src}").fetchone()[0],
            "horizons": _outcome_counts(con, src, spec["horizons"]),
        },
        "cards": {},
    }
    for h in spec["horizons"]:
        card_sql, link_sql = _cards_sql(
            channels, target, spec["label"], h, month, spec["weekly_horizons"]
        )
        cards = root / f"cards/{target}/{h}h/{month}.parquet"
        links = root / f"links/{target}/{h}h/{month}.parquet"
        card_rows = _copy_checked(con, card_sql, cards, "object_id, sensor_type, issued_at")
        link_rows = _copy_checked(con, link_sql, links, "event_id, sensor_type, issued_at")
        outcome = dict(
            con.execute(
                f"SELECT outcome, COUNT(*) FROM read_parquet({_literal(cards)}) GROUP BY 1"
            ).fetchall()
        )
        manifest["cards"][f"{h}h"] = {
            "rows": card_rows,
            "outcomes": {k: outcome.get(k, 0) for k in ("positive", "negative", "excluded")},
            "links": link_rows,
            "sha256": _digest(cards),
        }
    path = root / f"manifests/{target}/{month}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest

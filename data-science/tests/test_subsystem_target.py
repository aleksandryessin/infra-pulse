"""Synthetic point-in-time, coverage and censoring checks for subsystem labels."""

import hashlib

import duckdb
import pandas as pd
import pytest

from infra_pulse_research.modeling.subsystem_grid import materialize_registries, register_cutoffs
from infra_pulse_research.modeling.subsystem_target import (
    EXCLUSION_PRECEDENCE,
    label_month,
    materialize_target_tables,
    register_target_tables,
    split_period_sql,
)

GAS = "Газовая охрана"
X2021 = "excluded_2021_and_lookback"
FIRE = "Пожарная охрана"


def _connection(rows, *, uncovered=(), policy=()):
    """rows: (channel, object, system, sensor, timestamp, alarm)."""
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE current_enriched_events (
          channel_id BIGINT, object_id BIGINT, system_type VARCHAR, sensor_type VARCHAR,
          event_ts_local_raw VARCHAR, alarm BOOLEAN, is_epoch_placeholder BOOLEAN,
          sensor_name_current VARCHAR, object_kind VARCHAR, picket_from DOUBLE,
          picket_to DOUBLE, picket_form VARCHAR, picket_parsed BOOLEAN
        )
    """)
    con.executemany(
        "INSERT INTO current_enriched_events VALUES "
        "(?, ?, ?, ?, ?, ?, false, 'name', 'kind', NULL, NULL, NULL, false)",
        [list(row) for row in rows],
    )
    days = ", ".join(f"DATE '{day}'" for day in uncovered) or "DATE '1900-01-01'"
    con.execute(f"""
        CREATE VIEW coverage_days AS
        SELECT CAST(day AS DATE) AS day, CAST(day AS DATE) NOT IN ({days}) AS working_covered
        FROM generate_series(DATE '2018-12-01', DATE '2026-06-30', INTERVAL 1 DAY) t(day)
    """)
    con.execute("CREATE TABLE policy_days (day DATE, sensor_type_scope VARCHAR, reason VARCHAR)")
    for day, scope in policy:
        con.execute("INSERT INTO policy_days VALUES (?, ?, 'synthetic')", [day, scope])
    return con


def _labels(con, tmp_path, month="2024-03", name="run"):
    materialize_registries(con, tmp_path / name / "registry")
    files = materialize_target_tables(con, tmp_path / name / "target")
    register_cutoffs(con, month)
    manifest = label_month(con, month, tmp_path / name / "labels")
    frame = pd.read_parquet(
        files["alarm_timestamps"].parent.parent / f"labels/month={month}/labels.parquet"
    )
    return frame, manifest


def _row(frame, day, system=GAS, obj=10):
    hit = frame[
        (frame.issued_at == pd.Timestamp(day))
        & (frame.system_type == system)
        & (frame.object_id == obj)
    ]
    assert len(hit) == 1
    return hit.iloc[0]


BASE = [
    (1, 10, GAS, "Газовый датчик", "2024-02-20 10:00:00", False),
    (2, 10, GAS, "Газовый датчик", "2024-02-21 10:00:00", False),
    (3, 20, FIRE, "Датчик дыма", "2024-02-20 11:00:00", False),
]


def test_future_records_do_not_change_candidacy_or_known_channels(tmp_path):
    base, _ = _labels(_connection(BASE), tmp_path, name="base")
    future = BASE + [
        (4, 10, GAS, "Газовый датчик", "2024-03-10 08:00:00", True),
        (5, 30, FIRE, "Датчик дыма", "2024-03-15 00:00:00", False),
        (1, 10, GAS, "Газовый датчик", "2024-03-20 05:00:00", True),
    ]
    extended, _ = _labels(_connection(future), tmp_path, name="future")
    cols = ["object_id", "system_type", "issued_at", "known_channels", "split_period"]
    early = extended[extended.issued_at <= pd.Timestamp("2024-03-10")]
    pd.testing.assert_frame_equal(
        base[base.issued_at <= pd.Timestamp("2024-03-10")][cols].reset_index(drop=True),
        early[cols].reset_index(drop=True),
    )
    assert _row(extended, "2024-03-11").known_channels == 3
    # Strict first_seen < issued_at: a group first seen at midnight enters next day.
    group30 = extended[extended.object_id == 30].issued_at
    assert group30.min() == pd.Timestamp("2024-03-16")
    assert extended.duplicated(["object_id", "system_type", "issued_at"]).sum() == 0


def test_silent_covered_window_is_negative_not_unknown(tmp_path):
    frame, manifest = _labels(_connection(BASE), tmp_path)
    row = _row(frame, "2024-03-05")
    assert (row.outcome, row.y, row.exclusion_reason) == ("negative", 0, None)
    assert row.future_alarm_timestamps == 0 and pd.isna(row.first_alarm_at)
    assert manifest["outcomes"] == {"positive": 0, "negative": 62, "excluded": 0}
    assert manifest["rows"] == 62
    labels = tmp_path / "run/labels/month=2024-03/labels.parquet"
    assert manifest["sha256"] == hashlib.sha256(labels.read_bytes()).hexdigest()


def test_uncovered_and_group_scoped_policy_days_exclude(tmp_path):
    rows = BASE + [
        (1, 10, GAS, "Газовый датчик", "2024-03-07 03:00:00", True),
        (6, 20, FIRE, "Тепловой датчик", "2024-03-20 09:00:00", False),
    ]
    policy = [
        ("2024-03-12", "Газовый датчик"),
        ("2024-03-14", "Тепловой датчик"),
        ("2024-03-25", "Тепловой датчик"),
        ("2024-03-27", None),
    ]
    frame, manifest = _labels(_connection(rows, uncovered=["2024-03-07"], policy=policy), tmp_path)
    row = _row(frame, "2024-03-07")
    assert (row.outcome, row.exclusion_reason) == ("excluded", "source_coverage")
    assert pd.isna(row.y) and row.future_alarm_timestamps == 1
    assert _row(frame, "2024-03-06").outcome == "negative"
    assert _row(frame, "2024-03-12").exclusion_reason == "policy_day"
    assert _row(frame, "2024-03-12", FIRE, 20).outcome == "negative"
    # Heat channel is unknown before 2024-03-21, so its scoped day does not apply yet.
    assert _row(frame, "2024-03-14", FIRE, 20).outcome == "negative"
    assert _row(frame, "2024-03-25", FIRE, 20).exclusion_reason == "policy_day"
    assert _row(frame, "2024-03-25").outcome == "negative"
    assert _row(frame, "2024-03-27").exclusion_reason == "policy_day"
    assert manifest["exclusion_reasons"] == {"source_coverage": 2, "policy_day": 4}


def test_simultaneous_true_false_is_one_positive_conflict(tmp_path):
    rows = BASE + [
        (1, 10, GAS, "Газовый датчик", "2024-03-08 03:00:00", True),
        (1, 10, GAS, "Газовый датчик", "2024-03-08 03:00:00", True),
        (1, 10, GAS, "Газовый датчик", "2024-03-08 03:00:00", False),
        (2, 10, GAS, "Газовый датчик", "2024-03-08 03:00:00", False),
    ]
    frame, _ = _labels(_connection(rows), tmp_path)
    row = _row(frame, "2024-03-08")
    assert (row.outcome, row.y, row.lead_hours) == ("positive", 1, 3.0)
    assert row.future_alarm_rows == 2
    assert row.future_alarm_timestamps == 1
    assert row.future_alarm_channels == 1
    assert row.future_conflict_timestamps == 1
    assert len(frame) == 62
    assert frame.duplicated(["object_id", "system_type", "issued_at"]).sum() == 0


def test_episodes_strata_and_new_channel(tmp_path):
    rows = BASE + [
        (1, 10, GAS, "Газовый датчик", "2024-03-04 22:00:00", True),
        (1, 10, GAS, "Газовый датчик", "2024-03-05 05:00:00", True),
        (2, 10, GAS, "Газовый датчик", "2024-03-07 10:00:00", True),
        (1, 10, GAS, "Газовый датчик", "2024-03-14 10:00:00", True),
    ]
    con = _connection(rows)
    frame, manifest = _labels(con, tmp_path)
    episodes = con.sql(
        "SELECT episode_start, episode_end, alarm_rows, alarm_channels "
        "FROM subsystem_alarm_episodes ORDER BY episode_start"
    ).fetchall()
    assert [(e[2], e[3]) for e in episodes] == [(2, 1), (1, 1), (1, 1)]
    assert episodes[0][0] == pd.Timestamp("2024-03-04 22:00") and episodes[0][1] == pd.Timestamp(
        "2024-03-05 05:00"
    )
    first = _row(frame, "2024-03-04")
    assert (first.stratum, first.new_channel_alarm, first.episode_start_in_window) == (
        "onset_after_24h_quiet",
        True,
        True,
    )
    assert first.episode_start_at == pd.Timestamp("2024-03-04 22:00")
    second = _row(frame, "2024-03-05")
    assert (second.stratum, second.past24h_alarm, second.new_channel_alarm) == (
        "continuation",
        True,
        False,
    )
    assert not second.episode_start_in_window and second.episode_id is None
    third = _row(frame, "2024-03-07")
    assert (third.stratum, third.new_channel_alarm, third.episode_start_in_window) == (
        "onset_after_24h_quiet",
        True,
        True,
    )
    # Channel 1 last alarmed 9 days earlier: new within the 7-day rule.
    fourth = _row(frame, "2024-03-14")
    assert fourth.new_channel_alarm and fourth.episode_start_in_window
    negative = _row(frame, "2024-03-06")
    assert negative.past24h_alarm and pd.isna(negative.stratum)
    assert not negative.new_channel_alarm
    assert manifest["outcomes"]["positive"] == 4
    # Re-registering the persisted tables in a fresh connection gives the same labels.
    other = _connection(rows)
    materialize_registries(other, tmp_path / "again/registry")
    register_target_tables(other, tmp_path / "run/target")
    register_cutoffs(other, "2024-03")
    assert label_month(other, "2024-03", tmp_path / "again/labels")["sha256"] == manifest["sha256"]


@pytest.mark.parametrize(
    ("month", "issued_at", "split", "reason"),
    [
        ("2019-01", "2019-01-30 00:00:00", "insufficient_lookback", "insufficient_lookback"),
        ("2019-01", "2019-01-31 00:00:00", "development", None),
        ("2020-12", "2020-12-31 00:00:00", "development", None),
        ("2020-12", "2020-12-31 12:00:00", "development", "split_boundary"),
        ("2021-08", "2021-08-10 00:00:00", X2021, X2021),
        ("2022-01", "2022-01-30 00:00:00", X2021, X2021),
        ("2022-01", "2022-01-31 00:00:00", "development", None),
        ("2025-06", "2025-06-30 06:00:00", "validation_2025_h1", "split_boundary"),
        ("2026-06", "2026-06-30 00:00:00", "diagnostic_2026_june", None),
        ("2026-06", "2026-06-30 06:00:00", "diagnostic_2026_june", "data_end"),
    ],
)
def test_split_rules_and_2021(tmp_path, month, issued_at, split, reason):
    con = _connection([(1, 10, GAS, "Газовый датчик", "2018-12-15 10:00:00", False)])
    materialize_registries(con, tmp_path / "registry")
    materialize_target_tables(con, tmp_path / "target")
    con.execute(
        "CREATE TEMP VIEW custom_cutoffs AS SELECT 10::BIGINT AS object_id, "
        f"'{GAS}' AS system_type, TIMESTAMP '{issued_at}' AS issued_at"
    )
    label_month(con, month, tmp_path / "labels", cutoffs_view="custom_cutoffs")
    row = pd.read_parquet(tmp_path / f"labels/month={month}/labels.parquet").iloc[0]
    assert row.split_period == split
    assert row.exclusion_reason == reason
    assert row.outcome == ("negative" if reason is None else "excluded")
    assert reason is None or reason in EXCLUSION_PRECEDENCE


def test_split_sql_and_cutoff_validation(tmp_path):
    con = duckdb.connect()
    expr = split_period_sql("TIMESTAMP '2025-03-01'")
    assert con.sql(f"SELECT {expr}").fetchone()[0] == "validation_2025_h1"
    expr = split_period_sql("TIMESTAMP '2026-07-01'")
    assert con.sql(f"SELECT {expr}").fetchone()[0] == "after_data_end"
    con = _connection([(1, 10, GAS, "Газовый датчик", "2024-03-10 10:00:00", False)])
    materialize_registries(con, tmp_path / "registry")
    materialize_target_tables(con, tmp_path / "target")
    con.execute(
        "CREATE TEMP VIEW early AS SELECT 10::BIGINT AS object_id, "
        f"'{GAS}' AS system_type, TIMESTAMP '2024-03-05' AS issued_at"
    )
    with pytest.raises(ValueError, match="no channel known"):
        label_month(con, "2024-03", tmp_path / "labels", cutoffs_view="early")

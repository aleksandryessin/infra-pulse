"""Point-in-time checks of the v4 feature groups N/E/K/S/R/T on a synthetic journal."""

from __future__ import annotations

import datetime as dt
import json
import runpy
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from infra_pulse_research.modeling import sensor_failure_fe_v4 as fe
from infra_pulse_research.modeling.sensor_failure_target import (
    materialize_failure_tables,
    materialize_target_events,
    register_failure_tables,
)
from infra_pulse_research.modeling.subsystem_grid import materialize_registries

ROOT = Path(__file__).resolve().parents[2]
BUILD = runpy.run_path(
    str(ROOT / "data-science/scripts/build_sensor_failure_v4_features.py"), run_name="t"
)
CONFIG = json.loads(
    (ROOT / "data-science/configs/sensor_failure_tuning_v4.json").read_text(encoding="utf-8")
)
V2 = "configs/sensor_failure_model_v2.json"
MONTH = "2024-03"
CUT = "2024-03-15 00:00:00"
FIRE, DISP = "Пожарная охрана", "Диспетчерский контроль"
WATER, SEC = "Водоотведение", "Охранная подсистема"
SMOKE, PHASE = "Датчик дыма", "Состояние фазы"
PUMP, HATCH = "Состояние насоса", "КД Люк"
BAD, OK = "Неисправен", "Норма"


def _episode(channel, obj, sensor, system, at: str, minutes: int = 30, repeat: bool = False):
    start = dt.datetime.fromisoformat(at)
    rows = [(channel, obj, system, sensor, start, BAD)]
    if repeat:  # a second candidate record inside the episode (Hawkes input)
        rows.append((channel, obj, system, sensor, start + dt.timedelta(minutes=5), BAD))
    rows.append((channel, obj, system, sensor, start + dt.timedelta(minutes=minutes), OK))
    return rows


def _journal() -> list[tuple]:
    rows = []
    for ch, obj, sensor, system in (
        (1, 10, SMOKE, FIRE),
        (2, 10, SMOKE, FIRE),
        (3, 10, SMOKE, FIRE),
        (4, 10, SMOKE, FIRE),
        (5, 11, PHASE, DISP),
        (6, 11, PHASE, DISP),
    ):
        rows.append((ch, obj, system, sensor, dt.datetime(2024, 1, 1, 10), OK))
    # Channels 1-3 fail together three times before March: a node from the March snapshot.
    for day in ("2024-01-10", "2024-01-25", "2024-02-12"):
        for i, ch in enumerate((1, 2, 3)):
            rows += _episode(ch, 10, SMOKE, FIRE, f"{day} 09:0{i}:00", repeat=i == 0)
    for day in ("2024-02-05", "2024-02-20", "2024-03-12"):
        rows += _episode(4, 10, SMOKE, FIRE, f"{day} 11:00:00")
    # March: a node event before the cut, a cluster straddling the cut, events after it.
    for i, ch in enumerate((1, 2, 3)):
        rows += _episode(ch, 10, SMOKE, FIRE, f"2024-03-10 03:0{i}:00")
    rows += _episode(5, 11, PHASE, DISP, "2024-03-14 23:59:00", minutes=600)  # open at the cut
    rows += _episode(6, 11, PHASE, DISP, "2024-03-15 00:03:00")
    for i, ch in enumerate((1, 2)):
        rows += _episode(ch, 10, SMOKE, FIRE, f"2024-03-20 10:0{i}:00", repeat=True)
    rows += _episode(5, 11, PHASE, DISP, "2024-03-25 12:00:00")
    # Group W: a pump and a hatch of object 10; smoke channel 2 switched off.
    rows += [
        (7, 10, WATER, PUMP, dt.datetime(2024, 1, 1, 10), "Включен"),
        (7, 10, WATER, PUMP, dt.datetime(2024, 3, 9, 15), "Обесточен"),
        (7, 10, WATER, PUMP, dt.datetime(2024, 3, 9, 16), "Выключен"),  # actuator: not W
        (8, 10, SEC, HATCH, dt.datetime(2024, 1, 1, 10), "Замкнут"),
        (8, 10, SEC, HATCH, dt.datetime(2024, 3, 9, 20), "Не замкнут"),
        (2, 10, FIRE, SMOKE, dt.datetime(2024, 3, 9, 21), "Выключен"),
        (7, 10, WATER, PUMP, dt.datetime(2024, 3, 15, 1), "Обесточен"),  # after the cut
    ]
    return rows


def _connection(cut: str | None) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE current_enriched_events (
          channel_id BIGINT, object_id BIGINT, system_type VARCHAR, sensor_type VARCHAR,
          event_ts_local_raw VARCHAR, value_raw VARCHAR, value_numeric DOUBLE, alarm BOOLEAN,
          is_epoch_placeholder BOOLEAN, sensor_name_current VARCHAR, object_kind VARCHAR,
          picket_from DOUBLE, picket_to DOUBLE, picket_form VARCHAR, picket_parsed BOOLEAN)
    """)
    rows = [
        (
            ch,
            obj,
            system,
            sensor,
            at.strftime("%Y-%m-%d %H:%M:%S"),
            value,
            None,
            value == BAD,
            False,
            "n",
            "k",
            None,
            None,
            None,
            False,
        )
        for ch, obj, system, sensor, at, value in _journal()
        if cut is None or at < dt.datetime.fromisoformat(cut)
    ]
    con.executemany(
        "INSERT INTO current_enriched_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    con.execute("""
        CREATE VIEW coverage_days AS SELECT CAST(d AS DATE) AS day, true AS working_covered
        FROM generate_series(DATE '2023-01-01', DATE '2026-06-30', INTERVAL 1 DAY) g(d)
    """)
    con.execute("CREATE TABLE policy_days (day DATE, sensor_type_scope VARCHAR)")
    return con


def _build(tmp: Path, cut: str | None) -> dict[str, pd.DataFrame]:
    con = _connection(cut)
    materialize_registries(con, tmp / "registry")
    materialize_failure_tables(con, tmp / "tables")
    register_failure_tables(con, tmp / "tables", q_hours=24, w_minutes=10)
    materialize_target_events(con, "connection_loss_gt2s", tmp / "events", model_config=V2)
    reg, members, candidates = BUILD["load_inputs"](
        duckdb.connect(),
        registry=tmp / "registry/channel_registry.parquet",
        events_dir=tmp / "events/connection_loss_gt2s",
        tables=tmp / "tables",
        objects=None,
    )
    cal = CONFIG["groups"]["K"]["holidays"]
    month = fe.V4Month(
        duckdb.connect(),
        reg,
        members,
        candidates,
        MONTH,
        episode_filter=CONFIG["target"]["episode_filter"],
        holiday_dates=fe.holidays(range(2019, 2027), cal["month_days"]),
        signals=fe.work_signals(con, "current_enriched_events", "2025-01-01"),
    )
    frames = month.build()
    frames["nodes"] = month.nodes
    return frames


@pytest.fixture(scope="module")
def full(tmp_path_factory) -> dict[str, pd.DataFrame]:
    return _build(tmp_path_factory.mktemp("v4full"), None)


def _row(frame: pd.DataFrame, channel: int, day: str) -> pd.Series:
    hit = frame[(frame.channel_id == channel) & (frame.issued_at == pd.Timestamp(day))]
    assert len(hit) == 1
    return hit.iloc[0]


def test_truncated_journal_gives_identical_past_rows_in_every_group(full, tmp_path):
    cut = _build(tmp_path, CUT)
    edge = pd.Timestamp(CUT)
    for group in fe.GROUPS:
        a, b = full[group], cut[group]
        left = a[a.issued_at <= edge].sort_values(fe.KEY_COLS).reset_index(drop=True)
        right = b[b.issued_at <= edge].sort_values(fe.KEY_COLS).reset_index(drop=True)
        assert len(left) == 8 * 15
        pd.testing.assert_frame_equal(left, right, check_dtype=False, obj=group)
    # Power: after the cut the full journal adds information.
    later = full["E"][full["E"].issued_at > edge].reset_index(drop=True)
    trunc = cut["E"][cut["E"].issued_at > edge].reset_index(drop=True)
    assert not later.equals(trunc)


def test_node_features_use_the_union_of_the_node_channels(full):
    nodes = full["nodes"].set_index("channel_id")
    assert nodes.loc[1, "node_id"] == nodes.loc[3, "node_id"] == 1
    assert not nodes.loc[4, "in_node"] and not nodes.loc[5, "in_node"]
    n = full["N"]
    one = _row(n, 3, "2024-03-11")
    assert one.n4__in_node and one.n4__node_size == 3 and one.n4__node_events_7d == 1
    assert one.n4__hours_since_node_event == pytest.approx(21.0)
    assert one.n4__node_joint_share == pytest.approx(1.0)
    alone = _row(n, 4, "2024-03-11")
    assert not alone.n4__in_node and np.isnan(alone.n4__node_joint_share)
    # The card (object x sensor type) sees one group and the share of grouped channels.
    assert one.n4__card_groups == 1 and one.n4__card_share_in_node == pytest.approx(0.75)


def test_decayed_counters_intervals_and_hawkes(full):
    e = full["E"]
    row = _row(e, 2, "2024-03-11")
    age = (21 * 60 - 1) / 1440  # start 03-10 03:01, cutoff 03-11 00:00
    assert row.e4__ch_ewma_1d == pytest.approx(2 ** (-age), rel=1e-6)
    assert row.e4__node_ewma_1d == pytest.approx(2 ** (-21 / 24), rel=1e-6)
    assert row.e4__ch_ratio_7_30 < 1 and row.e4__ch_intervals == 3
    gaps = np.array([15, 18, 27], dtype=float)  # days between the channel's four starts
    assert row.e4__ch_interval_median_d == pytest.approx(np.median(gaps), rel=1e-3)
    assert row.e4__obj_hawkes_6h > 0
    quiet = _row(e, 6, "2024-03-11")
    assert quiet.e4__ch_ewma_30d == 0 and np.isnan(quiet.e4__ch_interval_median_d)


def test_calendar_network_relative_and_target_encoding(full):
    k = full["K"]
    assert _row(k, 1, "2024-03-08").k4__is_holiday
    assert _row(k, 1, "2024-03-07").k4__pre_day_off
    assert _row(k, 1, "2024-03-11").k4__after_holiday
    assert not _row(k, 1, "2024-03-11").k4__is_holiday
    s = full["S"]
    open_ = _row(s, 1, "2024-03-15")
    # Channel 5 is in an episode at the cutoff (started 23:59, not ended before t).
    assert open_.s4__net_active_share == pytest.approx(1 / 8)
    assert _row(s, 1, "2024-03-11").s4__net_objects_1d == 1
    assert np.isnan(open_.s4__area_objects_1d)  # no verified area in the synthetic registry
    r = full["R"]
    assert _row(r, 1, "2024-03-11").r4__obj_events_7d == 1
    assert _row(r, 1, "2024-03-11").r4__obj_events_7d_z > 0
    t = full["T"]
    assert _row(t, 1, "2024-03-11").t4__node_te > _row(t, 6, "2024-03-11").t4__node_te


def test_power_and_works_use_other_channels_of_the_object_in_the_past_day(full):
    w = full["W"]
    cols = [
        "w4__pumpfan_deenergized",
        "w4__pumpfan_deenergized_n",
        "w4__deenergized",
        "w4__switched_off_device",
        "w4__hatch_open",
    ]
    assert tuple(_row(w, 1, "2024-03-10")[cols]) == (True, 1, True, True, True)
    # Own records do not count; a pump switched off is an actuator, not a W signal.
    assert tuple(_row(w, 2, "2024-03-10")[cols]) == (True, 1, True, False, True)
    assert tuple(_row(w, 7, "2024-03-10")[cols]) == (False, 0, False, True, True)
    assert not _row(w, 5, "2024-03-10")[cols].astype(bool).any()  # other object
    assert not _row(w, 1, "2024-03-11")[cols].astype(bool).any()  # window [t − 24 h, t)


def test_every_group_has_prefixed_columns_one_row_per_grid_cell(full):
    for group, frame in full.items():
        if group == "nodes":
            continue
        assert not frame.duplicated(fe.KEY_COLS).any()
        cols = [c for c in frame.columns if c not in fe.KEY_COLS]
        assert cols and all(c.startswith(fe.PREFIX[group]) for c in cols)

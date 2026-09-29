"""Point-in-time checks of the v7 pair features on a synthetic journal."""

from __future__ import annotations

import datetime as dt
import runpy
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from infra_pulse_research.modeling import sensor_failure_evaluation as ev
from infra_pulse_research.modeling import sensor_failure_fe_v7 as fe7
from infra_pulse_research.modeling.sensor_failure_target import (
    label_target_month,
    materialize_failure_tables,
    materialize_target_events,
    register_failure_tables,
)
from infra_pulse_research.modeling.subsystem_grid import materialize_registries

ROOT = Path(__file__).resolve().parents[2]
BUILD = runpy.run_path(
    str(ROOT / "data-science/scripts/build_sensor_failure_v7_features.py"), run_name="t"
)
V5 = "configs/sensor_failure_model_v5.json"
TARGET = "connection_loss_ge2s"
RULE = {"op": ">=", "seconds": 2}
MONTHS = ("2024-02", "2024-03")
CUT = "2024-03-15 00:00:00"
FIRE, DISP = "Пожарная охрана", "Диспетчерский контроль"
WATER, SEC = "Водоотведение", "Охранная подсистема"
SMOKE, PHASE = "Датчик дыма", "Состояние фазы"
PUMP, HATCH = "Состояние насоса", "КД Люк"
BAD, OK = "Неисправен", "Норма"


def _episode(channel, obj, sensor, system, at: str, seconds: int = 1800):
    start = dt.datetime.fromisoformat(at)
    return [
        (channel, obj, system, sensor, start, BAD),
        (channel, obj, system, sensor, start + dt.timedelta(seconds=seconds), OK),
    ]


def _journal() -> list[tuple]:
    rows = []
    for ch, obj, sensor, system in (
        (1, 10, SMOKE, FIRE),
        (2, 10, SMOKE, FIRE),
        (3, 10, SMOKE, FIRE),
        (5, 11, PHASE, DISP),
        (6, 11, PHASE, DISP),
    ):
        rows.append((ch, obj, system, sensor, dt.datetime(2023, 12, 1, 10), OK))
    rows.append((7, 10, WATER, PUMP, dt.datetime(2023, 12, 1, 10), "Включен"))
    rows.append((8, 10, SEC, HATCH, dt.datetime(2023, 12, 1, 10), "Замкнут"))
    # Smoke pair of object 10: a mass-like event of three channels, single events, a
    # one-second episode (below the >= 2 s filter), a long episode.
    for i, ch in enumerate((1, 2, 3)):
        rows += _episode(ch, 10, SMOKE, FIRE, f"2024-01-20 09:0{i}:00")
    rows += _episode(1, 10, SMOKE, FIRE, "2024-02-10 11:00:00", seconds=1)
    rows += _episode(2, 10, SMOKE, FIRE, "2024-02-20 11:00:00", seconds=7200)
    rows += _episode(1, 10, SMOKE, FIRE, "2024-03-05 11:00:00")
    # Phase pair of object 11: an episode open across the cut, events after the cut.
    rows += _episode(5, 11, PHASE, DISP, "2024-02-25 12:00:00")
    rows += _episode(5, 11, PHASE, DISP, "2024-03-14 23:59:00", seconds=36000)
    rows += _episode(6, 11, PHASE, DISP, "2024-03-15 00:03:00")
    rows += _episode(6, 11, PHASE, DISP, "2024-03-20 10:00:00")
    rows += _episode(1, 10, SMOKE, FIRE, "2024-03-22 10:00:00")
    # Context: "Неопределен" on the smoke pair on 03-12, a message burst of object 11 on
    # 03-13, a pump without power on 03-09 and after the cut.
    rows.append((3, 10, FIRE, SMOKE, dt.datetime(2024, 3, 12, 8), "Неопределен"))
    rows.append((3, 10, FIRE, SMOKE, dt.datetime(2024, 3, 12, 9), OK))
    for k in range(6):
        rows.append((6, 11, DISP, PHASE, dt.datetime(2024, 3, 13, 10, k), OK))
    rows.append((7, 10, WATER, PUMP, dt.datetime(2024, 3, 9, 15), "Обесточен"))
    rows.append((7, 10, WATER, PUMP, dt.datetime(2024, 3, 15, 1), "Обесточен"))
    rows.append((8, 10, SEC, HATCH, dt.datetime(2024, 3, 16, 20), "Не замкнут"))
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


def _build(tmp: Path, cut: str | None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    con = _connection(cut)
    materialize_registries(con, tmp / "registry")
    materialize_failure_tables(con, tmp / "tables")
    register_failure_tables(con, tmp / "tables", q_hours=24, w_minutes=10)
    materialize_target_events(con, TARGET, tmp / "model/events", model_config=V5)
    for month in MONTHS:
        label_target_month(con, month, tmp / "model", target=TARGET, model_config=V5)
    reader = duckdb.connect()
    reg = BUILD["load_registry"](reader, tmp / "registry/channel_registry.parquet", None)
    members = BUILD["load_members"](reader, tmp / f"model/events/{TARGET}", end="2025-01-01")
    cards = pd.concat(
        [pd.read_parquet(tmp / f"model/cards/{TARGET}/168h/{m}.parquet") for m in MONTHS],
        ignore_index=True,
    )
    cards = cards.sort_values(fe7.KEYS).reset_index(drop=True)
    pair_days, object_days, signals = fe7.journal_day_aggregates(
        con, "current_enriched_events", "2025-01-01"
    )
    frame = fe7.PairFeatures(
        cards,
        members,
        reg,
        rule=RULE,
        pair_days=pair_days,
        object_days=object_days,
        signals=signals[["object_id", "ts"]],
        holiday_dates=fe7.holidays(range(2023, 2026)),
    ).build()
    return frame, cards, members


@pytest.fixture(scope="module")
def full(tmp_path_factory):
    return _build(tmp_path_factory.mktemp("v7full"), None)


def _row(frame: pd.DataFrame, obj: int, sensor: str, day: str) -> pd.Series:
    hit = frame[
        (frame.object_id == obj)
        & (frame.sensor_type == sensor)
        & (frame.issued_at == pd.Timestamp(day))
    ]
    assert len(hit) == 1
    return hit.iloc[0]


def test_truncated_journal_gives_identical_past_rows(full, tmp_path):
    cut, _, _ = _build(tmp_path, CUT)
    frame, _, _ = full
    edge = pd.Timestamp(CUT)
    left = frame[frame.issued_at <= edge].sort_values(fe7.KEYS).reset_index(drop=True)
    right = cut[cut.issued_at <= edge].sort_values(fe7.KEYS).reset_index(drop=True)
    assert len(left) > 0
    pd.testing.assert_frame_equal(left, right, check_dtype=False)
    # After the cut the full journal adds information.
    later = frame[frame.issued_at > edge].sort_values(fe7.KEYS).reset_index(drop=True)
    trunc = cut[cut.issued_at > edge].sort_values(fe7.KEYS).reset_index(drop=True)
    cols = [c for c in later.columns if c.startswith(("p7h__", "p7s__", "p7x__"))]
    assert not later[cols].equals(trunc[cols])


def test_history_count_is_the_static_list(full):
    frame, cards, members = full
    static = ev.static_list_scores(cards, members, RULE).to_numpy()
    assert np.array_equal(frame["p7h__events_365d"].to_numpy(), static)


def test_history_and_severity_semantics(full):
    frame, _, _ = full
    smoke = _row(frame, 10, SMOKE, "2024-03-06")
    # Events of the smoke pair known before 03-06: 01-20 (three channels), 02-20 (2 h)
    # and 03-05; the one-second episode of 02-10 is not a >= 2 s event.
    assert smoke.p7h__events_365d == 3 and smoke.p7h__events_7d == 1
    assert smoke.p7h__days_since_last == pytest.approx(13 / 24 - 2 / 86400, abs=1e-6)
    assert smoke.p7h__interval_median_d == pytest.approx(np.median([31 + 2 / 24, 14]), rel=1e-6)
    # Episodes that ended before t: 3 + 1 (1 s) + 1 (2 h) + 1 = 6.
    assert smoke.p7s__episodes_365d == 6
    assert smoke.p7s__share_lt2s_365d == pytest.approx(1 / 6)
    assert smoke.p7s__share_ge1h_365d == pytest.approx(1 / 6)
    assert smoke.p7s__dur_max_h_365d == pytest.approx(2.0)
    assert smoke.p7s__channels_per_event_365d == pytest.approx(5 / 3)
    assert smoke.p7s__last_open == 0 and smoke.p7s__last_duration_h == pytest.approx(0.5)
    # Decayed count: ages in days, half-life 30.
    ages = np.array(
        [
            (pd.Timestamp("2024-03-06") - pd.Timestamp(s)).total_seconds() / 86400
            for s in ("2024-01-20 09:00:02", "2024-02-20 11:00:02", "2024-03-05 11:00:02")
        ]
    )
    assert smoke.p7h__decay_30d == pytest.approx(np.exp2(-ages / 30).sum(), rel=1e-4)
    never = _row(frame, 11, PHASE, "2024-02-20")
    assert never.p7h__events_365d == 0 and np.isnan(never.p7h__days_since_last)


def test_active_episode_at_the_cut_is_open_and_not_a_duration(full):
    frame, _, _ = full
    phase = _row(frame, 11, PHASE, "2024-03-15")
    assert phase.p7c__active_channels == 1 and phase.p7s__last_open == 1
    assert np.isnan(phase.p7s__last_duration_h)
    # The open episode started at 23:59, so it counts as an event (known after 2 s); the
    # member of the same cluster that starts at 00:03 is not seen.
    assert phase.p7h__events_7d == 1 and phase.p7h__events_30d == 2
    assert phase.p7s__channels_per_event_365d == pytest.approx(1.0)


def test_context_flags_use_only_the_past_day(full):
    frame, _, _ = full
    assert _row(frame, 10, SMOKE, "2024-03-13").p7x__health_flag_1d == 1  # "Неопределен" 03-12
    assert _row(frame, 10, SMOKE, "2024-03-14").p7x__health_flag_1d == 0
    assert _row(frame, 11, PHASE, "2024-03-14").p7x__health_flag_1d == 1  # burst on 03-13
    assert _row(frame, 10, SMOKE, "2024-03-10").p7x__pumpfan_deenergized_1d == 1
    assert _row(frame, 10, SMOKE, "2024-03-11").p7x__pumpfan_deenergized_1d == 0
    assert _row(frame, 11, PHASE, "2024-03-10").p7x__pumpfan_deenergized_1d == 0  # other object
    # Object 10 has no events of other types; object 11 none either.
    assert _row(frame, 10, SMOKE, "2024-03-06").p7x__obj_other_events_30d == 0


def test_window_calendar_and_prefixes(full):
    frame, _, _ = full
    row = _row(frame, 10, SMOKE, "2024-03-04")  # Monday; 8 March is a holiday
    assert row.p7k__workdays_7d == 4 and row.p7k__holidays_7d == 1
    assert row.p7k__workdays_14d == 9
    assert not frame.duplicated(fe7.KEYS).any()
    feats = [c for c in frame.columns if c not in fe7.KEYS]
    assert all(c.startswith((*fe7.PREFIX.values(), "cat__")) for c in feats)


def test_within_day_rank_is_per_issue_day():
    frame = pd.DataFrame(
        {
            "issued_at": pd.to_datetime(["2024-01-01"] * 3 + ["2024-01-02"] * 2),
            "x": [1.0, 3.0, 2.0, 10.0, np.nan],
        }
    )
    ranked = fe7.within_day_rank(frame, ["x"])
    assert ranked["x"].tolist()[:3] == pytest.approx([1 / 3, 1.0, 2 / 3])
    assert ranked["x"].iloc[3] == 1.0 and np.isnan(ranked["x"].iloc[4])

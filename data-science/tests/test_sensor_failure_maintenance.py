"""Synthetic checks of the TO-session mask and the 'fire detector failure' target
(``sensor-failure-maintenance-v1``, definitions of 27.09.2026)."""

from __future__ import annotations

import pandas as pd

from infra_pulse_research import sensor_failure_maintenance as fm

MON = pd.Timestamp("2024-03-04")  # Monday
SAT = pd.Timestamp("2024-03-09")  # Saturday
SMOKE, HEAT, PHASE, DOOR = fm.SMOKE, fm.HEAT, "Состояние фазы", "КД Дверь"


def _rows(items):
    """items: (channel_id, object_id, sensor_type, value_raw, alarm, ts)."""
    return pd.DataFrame(items, columns=list(fm.ROW_COLUMNS))


def _test_session(start, obj=1, n=5, alarm=False, step_s=60, first_channel=100):
    """n smoke detectors: 'Обнаружен дым' then 'Дыма нет' 30 s later, one every step_s."""
    items = []
    for k in range(n):
        t = start + pd.Timedelta(seconds=step_s * k)
        ch = first_channel + k
        items.append((ch, obj, SMOKE, "Дыма нет", False, t - pd.Timedelta(hours=1)))
        items.append((ch, obj, SMOKE, "Обнаружен дым", alarm, t))
        items.append((ch, obj, SMOKE, "Дыма нет", False, t + pd.Timedelta(seconds=30)))
    return items


def _sessions(rows):
    return fm.detect_maintenance_sessions(fm.state_runs(_rows(rows)))


def test_state_runs_neutral_state_and_duration():
    runs = fm.state_runs(
        _rows(
            [
                (1, 1, SMOKE, "Дыма нет", False, MON),
                (1, 1, SMOKE, "Неопределен", False, MON + pd.Timedelta(minutes=1)),
                (1, 1, SMOKE, "Дыма нет", False, MON + pd.Timedelta(minutes=2)),
                (1, 1, SMOKE, "Обнаружен дым", True, MON + pd.Timedelta(minutes=3)),
                (1, 1, SMOKE, "Обнаружен дым", False, MON + pd.Timedelta(minutes=4)),
                (1, 1, SMOKE, "Дыма нет", False, MON + pd.Timedelta(minutes=5)),
            ]
        )
    )
    assert runs["state"].tolist() == ["Дыма нет", "Обнаружен дым", "Дыма нет"]
    assert runs["alarm_first"].tolist() == [False, True, False]
    assert runs.loc[1, "next_start"] - runs.loc[1, "start"] == pd.Timedelta(minutes=2)
    assert pd.isna(runs.loc[2, "next_start"])


def test_session_found_with_five_channels_and_not_with_four():
    found = _sessions(_test_session(MON + pd.Timedelta(hours=10)))
    assert len(found) == 1
    assert found.loc[0, "channels"] == 5
    assert found.loc[0, "session_start"] == MON + pd.Timedelta(hours=10)
    assert found.loc[0, "session_end"] == MON + pd.Timedelta(hours=10, minutes=4)
    assert _sessions(_test_session(MON + pd.Timedelta(hours=10), n=4)).empty


def test_session_needs_mostly_non_alarm_activations_and_gap_up_to_10_min():
    assert _sessions(_test_session(MON + pd.Timedelta(hours=10), alarm=True)).empty
    # gaps of 10 min keep one chain; 11 min split it into chains of < 5 channels
    assert len(_sessions(_test_session(MON + pd.Timedelta(hours=10), step_s=600))) == 1
    assert _sessions(_test_session(MON + pd.Timedelta(hours=10), step_s=660)).empty


def test_session_only_faults_is_not_a_session():
    items = [
        (200 + k, 1, SMOKE, fm.FAULT_STATE, True, MON + pd.Timedelta(hours=10, minutes=k))
        for k in range(6)
    ]
    assert _sessions(items).empty


def test_session_start_boundaries_07_19_and_weekend():
    assert len(_sessions(_test_session(MON + pd.Timedelta(hours=7)))) == 1
    assert _sessions(_test_session(MON + pd.Timedelta(hours=6, minutes=59))).empty
    assert len(_sessions(_test_session(MON + pd.Timedelta(hours=18, minutes=59)))) == 1
    assert _sessions(_test_session(MON + pd.Timedelta(hours=19))).empty
    assert _sessions(_test_session(SAT + pd.Timedelta(hours=10))).empty


def test_mask_plus_minus_30_min_inclusive_and_phase_not_masked():
    sessions = pd.DataFrame(
        {
            "object_id": [1],
            "session_start": [MON + pd.Timedelta(hours=10)],
            "session_end": [MON + pd.Timedelta(hours=11)],
        }
    )
    events = pd.DataFrame(
        {
            "object_id": [1, 1, 1, 1, 1, 2],
            "sensor_type": [SMOKE, SMOKE, HEAT, SMOKE, PHASE, SMOKE],
            "start_at": [
                MON + pd.Timedelta(hours=9, minutes=30),  # boundary: masked
                MON + pd.Timedelta(hours=9, minutes=29, seconds=59),  # outside
                MON + pd.Timedelta(hours=11, minutes=30),  # boundary: masked
                MON + pd.Timedelta(hours=11, minutes=30, seconds=1),  # outside
                MON + pd.Timedelta(hours=10, minutes=30),  # phase: never masked
                MON + pd.Timedelta(hours=10, minutes=30),  # other object
            ],
        }
    )
    out = fm.apply_maintenance_mask(events, sessions)
    assert out["maintenance_masked"].tolist() == [True, False, True, False, False, False]
    assert len(fm.apply_maintenance_mask(events, sessions, drop=True)) == 4
    assert not fm.apply_maintenance_mask(events, sessions.iloc[:0])["maintenance_masked"].any()


def _reset(ch, t, obj=1, st=SMOKE, dur_s=30, alarm=True):
    state = fm.SELF_RESET_STATE[st]
    back = "Дыма нет" if st == SMOKE else "Норма"
    return [
        (ch, obj, st, back, False, t - pd.Timedelta(hours=1)),
        (ch, obj, st, state, alarm, t),
        (ch, obj, st, back, False, t + pd.Timedelta(seconds=dur_s)),
    ]


def _events(rows, losses=None, sessions=None):
    runs = fm.state_runs(_rows(rows))
    sessions = fm.detect_maintenance_sessions(runs) if sessions is None else sessions
    losses = (
        losses
        if losses is not None
        else pd.DataFrame(columns=["object_id", "sensor_type", "start_at", "channel_id"])
    )
    return fm.fire_detector_events(losses, runs, sessions)


def test_self_reset_isolated_vs_neighbour_with_any_alarm():
    night = MON + pd.Timedelta(hours=2)
    alone = _events(_reset(1, night))
    assert alone["component"].tolist() == ["b1"]
    # a door opening with alarm = false 5 min later on the same object breaks isolation
    door = [
        (9, 1, DOOR, "Норма", False, night),
        (9, 1, DOOR, "Не замкнут", False, night + pd.Timedelta(minutes=5)),
    ]
    assert _events(_reset(1, night) + door).empty
    # the same door 11 min later does not
    door_late = [
        (9, 1, DOOR, "Норма", False, night),
        (9, 1, DOOR, "Не замкнут", False, night + pd.Timedelta(minutes=11)),
    ]
    assert len(_events(_reset(1, night) + door_late)) == 1
    # a neighbour on another object does not matter either
    other = [
        (8, 2, SMOKE, "Дыма нет", False, night),
        (8, 2, SMOKE, "Обнаружен дым", True, night + pd.Timedelta(minutes=1)),
    ]
    assert len(_events(_reset(1, night) + other).query("object_id == 1")) == 1


def test_self_reset_duration_limits_and_alarm_flag():
    night = MON + pd.Timedelta(hours=2)
    assert len(_events(_reset(1, night, dur_s=120))) == 1
    assert _events(_reset(1, night, dur_s=121)).empty
    assert len(_events(_reset(2, night, st=HEAT, dur_s=60))) == 1
    assert _events(_reset(2, night, st=HEAT, dur_s=61)).empty
    assert _events(_reset(1, night, alarm=False)).empty


def test_series_glue_72h_and_b2_flag():
    t0 = MON + pd.Timedelta(hours=2)
    rows = (
        _reset(1, t0)
        + _reset(1, t0 + pd.Timedelta(hours=20))  # glued, 20 h -> b2
        + _reset(1, t0 + pd.Timedelta(hours=20 + 72))  # glued, exactly 72 h
        + _reset(1, t0 + pd.Timedelta(hours=20 + 72 + 73))  # new series
    )
    ev = _events(rows)
    assert ev["component"].tolist() == ["b1", "b1"]
    assert ev["resets"].tolist() == [3, 1]
    assert ev["b2"].tolist() == [True, False]
    assert ev.loc[0, "start_at"] == t0


def test_connection_loss_masked_only_inside_session_and_self_reset_in_session_dropped():
    start = MON + pd.Timedelta(hours=10)
    rows = _test_session(start) + _reset(1, start + pd.Timedelta(minutes=20), obj=1)
    losses = pd.DataFrame(
        {
            "object_id": [1, 1, 1],
            "sensor_type": [SMOKE, SMOKE, PHASE],
            "start_at": [start + pd.Timedelta(minutes=10), start + pd.Timedelta(hours=3), start],
            "channel_id": [100, 101, 300],
        }
    )
    ev = _events(rows, losses)
    # in-session smoke loss masked, later smoke loss kept, the phase is outside the target;
    # the self-reset at 10:20 lies inside the session and is dropped
    assert ev["component"].tolist() == ["a"]
    assert ev.loc[0, "start_at"] == start + pd.Timedelta(hours=3)

"""Synthetic episode, cluster, censoring and leakage checks for sensor-failure labels."""

import copy
import json
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from infra_pulse_research.modeling.sensor_failure_target import (
    check_cutoff_hour,
    label_month,
    label_target_month,
    load_config,
    materialize_failure_tables,
    materialize_target_events,
    provisional_technical_sql,
    register_failure_tables,
    resolve_technical_sql,
)
from infra_pulse_research.modeling.subsystem_grid import materialize_registries

DATA_SCIENCE = Path(__file__).resolve().parents[1]
FIRE = "Пожарная охрана"
SMOKE = "Датчик дыма"
TEMP = "Датчик температуры"
BAD = "Неисправен"
OK = "Норма"

# channel 1: episodes, stuck continuation, neutral state, conflict
CH1 = [
    ("2024-02-20 10:00:00", OK),
    ("2024-03-02 10:00:00", BAD),
    ("2024-03-02 10:05:00", "Неопределен"),
    ("2024-03-02 12:00:00", OK),
    ("2024-03-02 20:00:00", BAD),
    ("2024-03-02 21:00:00", OK),
    ("2024-03-05 10:00:00", BAD),
    ("2024-03-05 10:00:00", OK),
    ("2024-03-05 10:30:00", OK),
    ("2024-03-08 10:00:00", BAD),
    ("2024-03-10 10:00:00", BAD),
    ("2024-03-11 10:00:00", OK),
]


def _rows(channel, obj, sensor, records, system=FIRE, numeric=None, alarm=False, epoch=False):
    return [
        (channel, obj, system, sensor, ts, value, numeric, alarm, epoch) for ts, value in records
    ]


def _connection(rows, *, uncovered=(), policy=()):
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE current_enriched_events (
          channel_id BIGINT, object_id BIGINT, system_type VARCHAR, sensor_type VARCHAR,
          event_ts_local_raw VARCHAR, value_raw VARCHAR, value_numeric DOUBLE, alarm BOOLEAN,
          is_epoch_placeholder BOOLEAN, sensor_name_current VARCHAR, object_kind VARCHAR,
          picket_from DOUBLE, picket_to DOUBLE, picket_form VARCHAR, picket_parsed BOOLEAN
        )
    """)
    for row in rows:
        con.execute(
            "INSERT INTO current_enriched_events VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, 'n', 'k', NULL, NULL, NULL, false)",
            list(row),
        )
    days = ", ".join(f"DATE '{d}'" for d in uncovered) or "DATE '1900-01-01'"
    con.execute(f"""
        CREATE VIEW coverage_days AS
        SELECT CAST(day AS DATE) AS day, CAST(day AS DATE) NOT IN ({days}) AS working_covered
        FROM generate_series(DATE '2018-12-01', DATE '2026-06-30', INTERVAL 1 DAY) t(day)
    """)
    con.execute("CREATE TABLE policy_days (day DATE, sensor_type_scope VARCHAR)")
    for day, scope in policy:
        con.execute("INSERT INTO policy_days VALUES (?, ?)", [day, scope])
    return con


def _build(tmp_path, rows, *, name="run", config=None, technical_sql=None, **kwargs):
    config = config or load_config()
    con = _connection(rows, **kwargs)
    materialize_registries(con, tmp_path / name / "registry")
    files = materialize_failure_tables(
        con,
        tmp_path / name / "tables",
        config=config,
        technical_sql=technical_sql or provisional_technical_sql,
    )
    return con, files, config


def _labels(con, tmp_path, config, *, month="2024-03", label="connection_loss", q=24, w=10):
    register_failure_tables(con, tmp_path / "run" / "tables", q_hours=q, w_minutes=w)
    manifest = label_month(
        con, month, tmp_path / "labels", label=label, q_hours=q, w_minutes=w, config=config
    )
    path = tmp_path / f"labels/{label}_q{q}h_w{w}m/month={month}/labels.parquet"
    return pd.read_parquet(path), manifest


def _episodes(files, key, label="connection_loss"):
    frame = pd.read_parquet(files[key])
    return frame[frame.label == label].sort_values("start_at").reset_index(drop=True)


def _row(frame, day, channel=1):
    hit = frame[(frame.issued_at == pd.Timestamp(day)) & (frame.channel_id == channel)]
    assert len(hit) == 1
    return hit.iloc[0]


def test_episode_start_end_q_neutral_and_conflict(tmp_path):
    con, files, config = _build(tmp_path, _rows(1, 10, SMOKE, CH1))
    ep = _episodes(files, "episodes_q24h")
    assert ep.start_at.tolist() == [
        pd.Timestamp("2024-03-02 10:00"),
        pd.Timestamp("2024-03-05 10:00"),
        pd.Timestamp("2024-03-08 10:00"),
    ]
    # Neutral 'Неопределен' does not end the first episode; the conflict timestamp
    # (candidate and clean in one second) does not end the second one.
    assert ep.end_at.tolist() == [
        pd.Timestamp("2024-03-02 12:00"),
        pd.Timestamp("2024-03-05 10:30"),
        pd.Timestamp("2024-03-11 10:00"),
    ]
    assert ep.start_conflict.tolist() == [False, True, False]
    assert ep.duration_hours.tolist() == [2.0, 0.5, 72.0]
    # 03-10 is >= Q after 03-08 but no clean record in between: same episode.
    assert ep.candidate_timestamps.tolist() == [2, 1, 2]
    assert _episodes(files, "episodes_q168h").start_at.tolist() == [
        pd.Timestamp("2024-03-02 10:00")
    ]
    ended = _episodes(files, "episodes_q24h", "connection_loss_end_undetermined")
    assert ended.end_at.iloc[0] == pd.Timestamp("2024-03-02 10:05")


def test_channel_day_labels_active_shadow_and_duration(tmp_path):
    con, _, config = _build(tmp_path, _rows(1, 10, SMOKE, CH1))
    frame, manifest = _labels(con, tmp_path, config)
    first = _row(frame, "2024-03-02")
    assert (first.outcome_24h, first.y_24h, first.lead_hours_24h) == ("positive", 1, 10.0)
    assert first.y_24h_dur1h == 1
    after = _row(frame, "2024-03-03")
    assert not after.active_episode and after.in_q_shadow and after.candidate_past_7d
    assert (after.y_24h, after.y_48h, after.y_168h) == (0, 0, 1)
    assert after.starts_168h == 2
    short = _row(frame, "2024-03-05")
    assert (short.y_24h, short.y_24h_dur1h) == (1, 0)
    for day in ("2024-03-09", "2024-03-10", "2024-03-11"):
        assert _row(frame, day).exclusion_reason_24h == "active_episode"
    assert _row(frame, "2024-03-12").outcome_24h == "negative"
    assert manifest["rows"] == 31 and frame.duplicated(["channel_id", "issued_at"]).sum() == 0


def test_future_records_do_not_change_past_state(tmp_path):
    con, _, config = _build(tmp_path, _rows(1, 10, SMOKE, CH1))
    full, _ = _labels(con, tmp_path, config)
    cut = [r for r in CH1 if r[0] < "2024-03-10"]
    con2, _, _ = _build(tmp_path, _rows(1, 10, SMOKE, cut), name="cut")
    register_failure_tables(con2, tmp_path / "cut" / "tables", q_hours=24, w_minutes=10)
    label_month(
        con2,
        "2024-03",
        tmp_path / "cut_labels",
        label="connection_loss",
        q_hours=24,
        w_minutes=10,
        config=config,
    )
    short = pd.read_parquet(
        tmp_path / "cut_labels/connection_loss_q24h_w10m/month=2024-03/labels.parquet"
    )
    cols = [
        "issued_at",
        "known",
        "active_episode",
        "last_candidate_at",
        "candidate_past_7d",
        "in_q_shadow",
        "last_start_at",
    ]
    early = full.issued_at <= pd.Timestamp("2024-03-10")
    pd.testing.assert_frame_equal(
        full[early][cols].reset_index(drop=True),
        short[short.issued_at <= pd.Timestamp("2024-03-10")][cols].reset_index(drop=True),
    )


def test_empty_covered_window_is_zero_and_gaps_exclude(tmp_path):
    rows = _rows(2, 10, SMOKE, [("2024-02-20 10:00:00", OK)]) + _rows(
        3, 11, "Газовый датчик", [("2024-02-20 10:00:00", OK)], system="Газовая охрана"
    )
    policy = [("2024-03-25", SMOKE)]
    con, _, config = _build(tmp_path, rows, uncovered=["2024-03-20"], policy=policy)
    frame, _ = _labels(con, tmp_path, config)
    quiet = _row(frame, "2024-03-05", 2)
    assert (quiet.outcome_24h, quiet.y_24h, quiet.y_168h) == ("negative", 0, 0)
    assert _row(frame, "2024-03-19", 2).outcome_24h == "negative"
    assert _row(frame, "2024-03-19", 2).exclusion_reason_48h == "source_coverage"
    assert _row(frame, "2024-03-20", 2).exclusion_reason_24h == "source_coverage"
    assert _row(frame, "2024-03-21", 2).exclusion_reason_24h == "lookback_coverage"
    assert _row(frame, "2024-03-22", 2).outcome_24h == "negative"
    assert _row(frame, "2024-03-25", 2).exclusion_reason_24h == "policy_day"
    assert _row(frame, "2024-03-25", 3).outcome_24h == "negative"
    assert _row(frame, "2024-03-24", 2).exclusion_reason_48h == "policy_day"


def test_mass_clusters_chain_within_w(tmp_path):
    rows = []
    for channel, minute in ((3, 0), (4, 8), (5, 16)):
        rows += _rows(
            channel,
            20,
            SMOKE,
            [("2024-02-20 10:00:00", OK), (f"2024-03-15 00:{minute:02d}:00", BAD)],
        )
    rows += _rows(6, 21, SMOKE, [("2024-02-20 10:00:00", OK), ("2024-03-15 00:04:00", BAD)])
    con, files, config = _build(tmp_path, rows)
    ep = _episodes(files, "episodes_q24h")
    obj20 = ep[ep.object_id == 20]
    assert obj20.cluster_size_w10.tolist() == [3, 3, 3]
    assert obj20.cluster_id_w10.nunique() == 1
    assert obj20.cluster_size_w1.tolist() == [1, 1, 1]
    assert obj20.cluster_size_w60.tolist() == [3, 3, 3]
    assert ep[ep.object_id == 21].cluster_size_w10.tolist() == [1]
    clusters = pd.read_parquet(files["clusters_q24h_w10m"])
    clusters = clusters[clusters.label == "connection_loss"]
    assert sorted(clusters["size"].tolist()) == [1, 3]
    assert clusters[clusters["size"] == 3].span_minutes.iloc[0] == 16.0
    frame, _ = _labels(con, tmp_path, config)
    assert _row(frame, "2024-03-15", 4).cluster_size_24h == 3


def test_scope_no_prior_history_and_label_variants(tmp_path):
    rows = _rows(7, 30, SMOKE, [("2024-03-10 12:00:00", OK)])
    rows += _rows(
        8, 30, "Состояние насоса", [("2024-02-20 10:00:00", OK), ("2024-03-12 10:00:00", BAD)]
    )
    rows += _rows(
        9, 30, SMOKE, [("2024-02-20 10:00:00", OK), ("2024-03-12 10:00:00", "Отключено устройство")]
    )
    con, files, config = _build(tmp_path, rows)
    ep = pd.read_parquet(files["episodes_q24h"])
    assert set(ep[ep.channel_id == 8].label) == {"connection_loss_s2"}
    assert set(ep[ep.channel_id == 9].label) == {"connection_loss_s1", "connection_loss_s2"}
    frame, _ = _labels(con, tmp_path, config)
    assert 8 not in set(frame.channel_id)
    assert _row(frame, "2024-03-10", 7).exclusion_reason_24h == "no_prior_history"
    assert _row(frame, "2024-03-11", 7).outcome_24h == "negative"


def test_injected_technical_predicate_and_split_rules(tmp_path):
    config = copy.deepcopy(load_config())
    rows = _rows(
        1,
        40,
        TEMP,
        [("2018-12-15 10:00:00", "20")],
        system="Температурная подсистема",
        numeric=20.0,
    )
    rows += _rows(
        1,
        40,
        TEMP,
        [("2024-12-27 03:00:00", "-999")],
        system="Температурная подсистема",
        numeric=-999.0,
    )
    con, files, _ = _build(
        tmp_path,
        rows,
        config=config,
        technical_sql=lambda a: f"CASE WHEN {a}.value_numeric = -999 THEN 'code' END",
    )
    ep = pd.read_parquet(files["episodes_q24h"])
    assert ep[ep.label == "technical_value"].start_at.tolist() == [pd.Timestamp("2024-12-27 03:00")]
    frame, _ = _labels(con, tmp_path, config, month="2024-12", label="technical_value")
    row = _row(frame, "2024-12-26")
    assert (row.y_24h, row.y_48h, row.exclusion_reason_168h) == (0, 1, "split_boundary")
    frame, _ = _labels(con, tmp_path, config, month="2021-08", label="technical_value")
    assert set(frame.exclusion_reason_24h) == {"excluded_2021_and_lookback"}
    with pytest.raises(ValueError, match="registered episodes"):
        label_month(
            con,
            "2021-08",
            tmp_path / "x",
            label="technical_value",
            q_hours=168,
            w_minutes=10,
            config=config,
        )


def test_real_provider_epoch_sensitivity_and_timestamp_alarm(tmp_path):
    provider, name = resolve_technical_sql()
    assert name.startswith("technical_reason_sql")
    temp = "Температурная подсистема"
    rows = _rows(1, 50, TEMP, [("2024-02-20 10:00:00", "21.5")], system=temp, numeric=21.5)
    # A -127 numeric row and its paired alarm-flagged text row share one timestamp.
    rows += _rows(1, 50, TEMP, [("2024-03-03 04:00:00", "-127")], system=temp, numeric=-127.0)
    rows += _rows(1, 50, TEMP, [("2024-03-03 04:00:00", "Не определено")], system=temp, alarm=True)
    rows += _rows(1, 50, TEMP, [("2024-03-03 04:00:00", BAD)], system=temp)
    rows += _rows(1, 50, TEMP, [("2024-03-03 05:00:00", "22")], system=temp, numeric=22.0)
    security = "Охранная подсистема"
    rows += _rows(
        2, 50, "Состояние охраны", [("2024-02-20 10:00:00", "Под охраной")], system=security
    )
    # The epoch is the value; the timestamp parses and the row is kept.
    epoch_value = [("2024-03-04 10:00:00", "01.01.1970 03:00:00")]
    rows += _rows(2, 50, "Состояние охраны", epoch_value, system=security, epoch=True)
    con, files, _ = _build(tmp_path, rows, technical_sql=provider)
    ep = pd.read_parquet(files["episodes_q24h"])
    main = ep[ep.label == "technical_value"]
    assert main.channel_id.tolist() == [1]
    assert main.start_alarm_at_ts.tolist() == [True]
    assert not main.start_candidate_alarm.iloc[0] or main.start_candidate_rows.iloc[0] == 2
    assert main.end_at.tolist() == [pd.Timestamp("2024-03-03 05:00")]
    epoch = ep[ep.label == "technical_value_epoch"]
    assert sorted(epoch.channel_id) == [1, 2]
    assert ep[ep.label == "connection_loss"].channel_id.tolist() == [1]


def _model(
    tmp_path,
    rows,
    *,
    target="connection_loss_1h",
    months=("2024-03",),
    name="run",
    model_config=None,
    cutoff_hour=None,
    **kwargs,
):
    con, files, config = _build(tmp_path, rows, name=name, **kwargs)
    register_failure_tables(con, tmp_path / name / "tables", q_hours=24, w_minutes=10)
    out = tmp_path / name / "model"
    materialize_target_events(con, target, out / "events", config=config, model_config=model_config)
    manifests = [
        label_target_month(
            con,
            m,
            out,
            target=target,
            config=config,
            model_config=model_config,
            cutoff_hour=cutoff_hour,
        )
        for m in months
    ]

    def read(kind, horizon=None):
        part = f"{kind}/{target}" + (f"/{horizon}h" if horizon else "")
        return pd.read_parquet(out / part)

    return read, manifests


def test_model_protocol_duration_filter_q_shadow_and_candidates(tmp_path):
    read, manifests = _model(tmp_path, _rows(1, 10, SMOKE, CH1))
    ch = read("channels")
    assert (_row(ch, "2024-03-02").y_24h, _row(ch, "2024-03-05").y_24h) == (1, 0)
    assert _row(ch, "2024-03-08").y_24h == 1
    shadow = _row(ch, "2024-03-03")
    assert (shadow.exclusion_reason_24h, shadow.candidate) == ("q_shadow", False)
    assert _row(ch, "2024-03-06").exclusion_reason_24h == "q_shadow"
    assert _row(ch, "2024-03-09").exclusion_reason_24h == "active_episode"
    assert _row(ch, "2024-03-12").candidate and _row(ch, "2024-03-12").outcome_24h == "negative"
    assert _row(ch, "2024-03-05").exclusion_reason_168h == "not_weekly_cutoff"
    assert _row(ch, "2024-03-05").exclusion_reason_24h is None
    assert manifests[0]["channels"]["rows"] == 31
    everything, _ = _model(
        tmp_path, _rows(1, 10, SMOKE, CH1), target="connection_loss_all", name="all"
    )
    assert _row(everything("channels"), "2024-03-05").y_24h == 1


def test_model_protocol_future_records_do_not_change_eligibility(tmp_path):
    full, _ = _model(tmp_path, _rows(1, 10, SMOKE, CH1))
    cut = [r for r in CH1 if r[0] < "2024-03-10"]
    short, _ = _model(tmp_path, _rows(1, 10, SMOKE, cut), name="cut")
    a, b = full("channels"), short("channels")
    past = [
        "issued_at",
        "known",
        "candidate",
        "active_episode",
        "in_q_shadow",
        "last_candidate_at",
        "last_start_at",
        "last_end_at",
        "hours_since_last_candidate",
    ]
    early = pd.Timestamp("2024-03-10")
    pd.testing.assert_frame_equal(
        a[a.issued_at <= early][past].reset_index(drop=True),
        b[b.issued_at <= early][past].reset_index(drop=True),
    )
    # Labels whose window plus the 1 h duration check ends before the cut agree.
    for horizon, last in ((24, "2024-03-08"), (48, "2024-03-07")):
        cols = ["issued_at", f"y_{horizon}h", f"exclusion_reason_{horizon}h"]
        keep_a, keep_b = a.issued_at <= pd.Timestamp(last), b.issued_at <= pd.Timestamp(last)
        pd.testing.assert_frame_equal(
            a[keep_a][cols].reset_index(drop=True), b[keep_b][cols].reset_index(drop=True)
        )


CROSS = (
    _rows(
        3,
        20,
        SMOKE,
        [("2024-02-20 10:00:00", OK), ("2024-03-14 23:55:00", BAD), ("2024-03-15 01:55:00", OK)],
    )
    + _rows(
        4,
        20,
        SMOKE,
        [("2024-02-20 10:00:00", OK), ("2024-03-15 00:03:00", BAD), ("2024-03-15 02:03:00", OK)],
    )
    + _rows(5, 20, SMOKE, [("2024-02-20 10:00:00", OK)])
    + _rows(6, 20, "Датчик движения", [("2024-02-20 10:00:00", OK)], system="Охранная подсистема")
)


def test_cluster_crossing_cutoff_cards_and_links(tmp_path):
    read, _ = _model(tmp_path, CROSS)
    ch = read("channels")
    assert _row(ch, "2024-03-14", 3).y_24h == 1 and _row(ch, "2024-03-15", 4).y_24h == 1
    cards = read("cards", 24)
    smoke = cards[(cards.sensor_type == SMOKE) & (cards.object_id == 20)].set_index("issued_at")
    day, next_day = smoke.loc[pd.Timestamp("2024-03-14")], smoke.loc[pd.Timestamp("2024-03-15")]
    assert (day.y_card, day.candidate_channels, day.known_channels) == (1, 3, 3)
    assert day.lead_hours == pytest.approx(23 + 55 / 60)
    # The event started before the next cutoff: not a forecast target there.
    assert (next_day.y_card, next_day.candidate_channels) == (0, 2)
    motion = cards[(cards.sensor_type == "Датчик движения")]
    assert motion.y_card.max() == 0 and len(motion) == 31
    links = read("links", 24)
    assert links.issued_at.tolist() == [pd.Timestamp("2024-03-14")]
    assert links.candidate_member.tolist() == [True]
    cards48 = read("cards", 48)
    hit48 = cards48[(cards48.sensor_type == SMOKE) & (cards48.y_card == 1)].issued_at
    assert sorted(hit48) == [pd.Timestamp("2024-03-13"), pd.Timestamp("2024-03-14")]
    weekly = read("cards", 168)
    assert set(weekly.issued_at.dt.dayofweek) == {0}
    assert weekly[(weekly.sensor_type == SMOKE) & (weekly.y_card == 1)].issued_at.tolist() == [
        pd.Timestamp("2024-03-11")
    ]
    events = pd.read_parquet(tmp_path / "run/model/events/connection_loss_1h/events.parquet")
    assert (len(events), events["size"].iloc[0], events.qualifies.iloc[0]) == (1, 2, True)


def test_event_qualifies_through_any_member(tmp_path):
    rows = _rows(
        7,
        30,
        "Датчик движения",
        [("2024-02-20 10:00:00", OK), ("2024-03-20 10:00:00", BAD), ("2024-03-20 10:06:00", OK)],
        system="Охранная подсистема",
    )
    rows += _rows(
        8,
        30,
        SMOKE,
        [("2024-02-20 10:00:00", OK), ("2024-03-20 10:05:00", BAD), ("2024-03-20 13:05:00", OK)],
    )
    read, _ = _model(tmp_path, rows)
    ch = read("channels")
    assert (_row(ch, "2024-03-20", 7).y_24h, _row(ch, "2024-03-20", 8).y_24h) == (0, 1)
    cards = read("cards", 24)
    hit = cards[cards.issued_at == pd.Timestamp("2024-03-20")].set_index("sensor_type")
    assert hit.loc["Датчик движения"].y_card == 1 and hit.loc[SMOKE].y_card == 1
    events = pd.read_parquet(tmp_path / "run/model/events/connection_loss_1h/events.parquet")
    assert events.qualifying_members.tolist() == [1]
    assert list(events.sensor_types.iloc[0]) == sorted(["Датчик движения", SMOKE])


def test_model_periods(tmp_path):
    rows = _rows(1, 40, SMOKE, [("2018-12-15 10:00:00", OK)])
    read, _ = _model(tmp_path, rows, months=("2019-01", "2025-06", "2025-08"))
    ch = read("channels")
    assert _row(ch, "2019-01-30").split_period == "excluded"
    assert _row(ch, "2019-01-30").exclusion_reason_24h == "insufficient_lookback"
    assert _row(ch, "2019-01-31").split_period == "development"
    assert _row(ch, "2025-06-30").split_period == "calibration_and_threshold"
    assert _row(ch, "2025-06-30").exclusion_reason_48h == "split_boundary"
    assert _row(ch, "2025-08-01").split_period == "holdout"


def test_later_episode_clean_record_does_not_end_an_earlier_episode(tmp_path):
    """Audit F1: a clean record inside a later anchor's span must not close an earlier
    episode whose own first clean record lies beyond the first bounded pass."""
    rows = [
        ("2024-02-20 10:00:00", OK),
        ("2024-03-01 10:00:00", BAD),
        ("2024-03-04 10:00:00", OK),
        ("2024-03-20 10:00:00", BAD),
        ("2024-03-20 12:00:00", OK),
    ]
    full, _ = _model(tmp_path, _rows(1, 10, SMOKE, rows))
    cut, _ = _model(
        tmp_path, _rows(1, 10, SMOKE, [r for r in rows if r[0] < "2024-03-15"]), name="cut"
    )
    for read in (full, cut):
        state = _row(read("channels"), "2024-03-10")
        assert (bool(state.active_episode), bool(state.candidate)) == (False, True)
        assert state.last_end_at == pd.Timestamp("2024-03-04 10:00:00")
    _, files, _ = _build(tmp_path, _rows(1, 10, SMOKE, rows), name="episodes")
    episodes = _episodes(files, "episodes_q24h")
    assert list(episodes.start_at) == [
        pd.Timestamp("2024-03-01 10:00:00"),
        pd.Timestamp("2024-03-20 10:00:00"),
    ]
    assert episodes.end_at.iloc[0] == pd.Timestamp("2024-03-04 10:00:00")


V2 = "configs/sensor_failure_model_v2.json"


def test_v2_gt2s_filter_boundary(tmp_path):
    rows = _rows(
        1,
        10,
        SMOKE,
        [("2024-02-20 10:00:00", OK), ("2024-03-05 10:00:00", BAD), ("2024-03-05 10:00:02", OK)],
    )
    rows += _rows(
        2,
        10,
        SMOKE,
        [("2024-02-20 10:00:00", OK), ("2024-03-12 10:00:00", BAD), ("2024-03-12 10:00:03", OK)],
    )
    rows += _rows(3, 11, SMOKE, [("2024-02-20 10:00:00", OK), ("2024-03-20 10:00:00", BAD)])
    read, manifests = _model(tmp_path, rows, target="connection_loss_gt2s", model_config=V2)
    ch = read("channels")
    assert _row(ch, "2024-03-05", 1).y_24h == 0  # exactly 2 s: excluded
    assert _row(ch, "2024-03-12", 2).y_24h == 1  # 3 s: included
    assert _row(ch, "2024-03-20", 3).y_24h == 1  # open at data end
    assert {"y_72h", "outcome_72h"} <= set(ch.columns) and "y_168h" not in ch.columns
    assert ch.exclusion_reason_72h.ne("not_weekly_cutoff").all()
    assert manifests[0]["model_version"] == "sensor-failure-model-v2"
    assert sorted(read("cards", 72).issued_at.dt.dayofweek.unique()) == list(range(7))
    events = pd.read_parquet(tmp_path / "run/model/events/connection_loss_gt2s/events.parquet")
    assert events.sort_values("event_start").qualifies.tolist() == [False, True, True]
    everything, _ = _model(
        tmp_path, rows, target="connection_loss_all", model_config=V2, name="all"
    )
    assert _row(everything("channels"), "2024-03-05", 1).y_24h == 1


def test_v2_eligibility_and_labels_match_v1(tmp_path):
    v1, _ = _model(tmp_path, _rows(1, 10, SMOKE, CH1) + CROSS)
    v2, _ = _model(tmp_path, _rows(1, 10, SMOKE, CH1) + CROSS, name="v2", model_config=V2)
    a, b = v1("channels"), v2("channels")
    keys = ["channel_id", "issued_at"]
    cols = keys + [
        "known",
        "candidate",
        "active_episode",
        "in_q_shadow",
        "last_candidate_at",
        "last_end_at",
        "split_period",
    ]
    for h in (24, 48):
        cols += [f"y_{h}h", f"exclusion_reason_{h}h", f"first_start_{h}h", f"cluster_id_{h}h"]
    pd.testing.assert_frame_equal(
        a.sort_values(keys)[cols].reset_index(drop=True),
        b.sort_values(keys)[cols].reset_index(drop=True),
    )
    card_cols = [
        "object_id",
        "sensor_type",
        "issued_at",
        "y_card",
        "candidate_channels",
        "exclusion_reason",
    ]
    pd.testing.assert_frame_equal(
        v1("cards", 24).sort_values(card_cols[:3])[card_cols].reset_index(drop=True),
        v2("cards", 24).sort_values(card_cols[:3])[card_cols].reset_index(drop=True),
    )


# --- v3 branch A: daily issue at 06:00 ------------------------------------------------

NIGHT = _rows(
    2,
    10,
    SMOKE,
    [("2024-02-20 10:00:00", OK), ("2024-03-15 03:00:00", BAD), ("2024-03-15 05:00:00", OK)],
)


def test_cutoff_0600_window_shadow_and_night_start_is_history(tmp_path):
    rows = _rows(1, 10, SMOKE, CH1) + NIGHT
    six, manifests = _model(tmp_path, rows, name="six", cutoff_hour=6)
    zero, zero_manifests = _model(tmp_path, rows, name="zero")
    ch6, ch0 = six("channels"), zero("channels")
    assert set(ch6.issued_at.dt.hour) == {6} and set(ch0.issued_at.dt.hour) == {0}
    assert len(ch6) == len(ch0) == 62
    first = _row(ch6, "2024-03-02 06:00")
    assert (first.y_24h, first.lead_hours_24h) == (1, 4.0)
    assert _row(ch6, "2024-03-03 06:00").exclusion_reason_24h == "q_shadow"
    assert _row(ch6, "2024-03-04 06:00").outcome_24h == "negative"
    assert _row(ch6, "2024-03-08 06:00").y_24h == 1
    for day in ("2024-03-09 06:00", "2024-03-11 06:00"):  # ends 03-11 10:00
        assert _row(ch6, day).exclusion_reason_24h == "active_episode"
    assert _row(ch6, "2024-03-12 06:00").candidate
    # A start between midnight and 06:00 is the label of the previous 06:00 cutoff and
    # history (Q shadow) at 06:00 of its own day; the midnight protocol labels it.
    night0 = _row(ch0, "2024-03-15", 2)
    assert (night0.y_24h, night0.lead_hours_24h) == (1, 3.0)
    night6 = _row(ch6, "2024-03-15 06:00", 2)
    assert (night6.exclusion_reason_24h, bool(night6.candidate)) == ("q_shadow", False)
    assert pd.isna(night6.y_24h)
    before = _row(ch6, "2024-03-14 06:00", 2)
    assert (before.y_24h, before.lead_hours_24h) == (1, 21.0)
    cards = six("cards", 24).set_index("issued_at")
    assert cards.loc[pd.Timestamp("2024-03-14 06:00")].y_card == 1
    day = cards.loc[pd.Timestamp("2024-03-15 06:00")]
    assert (day.y_card, day.candidate_channels) == (0, 1)
    assert manifests[0]["cutoff_hour"] == 6 and "cutoff_hour" not in zero_manifests[0]


def test_cutoff_0600_coverage_policy_and_censoring(tmp_path):
    rows = _rows(2, 10, SMOKE, [("2024-02-20 10:00:00", OK)])
    kwargs = {"uncovered": ["2024-03-21"], "policy": [("2024-03-25", SMOKE)]}
    months = ("2024-03", "2024-12", "2026-06")
    six, _ = _model(tmp_path, rows, name="six", cutoff_hour=6, months=months, **kwargs)
    zero, _ = _model(tmp_path, rows, name="zero", months=months, **kwargs)
    ch6, ch0 = six("channels"), zero("channels")

    def reason(frame, day, h=24):
        return _row(frame, day, 2)[f"exclusion_reason_{h}h"]

    # The 06:00 window [t, t + 24 h) also touches the next calendar day.
    assert reason(ch6, "2024-03-19 06:00") is None
    assert reason(ch6, "2024-03-19 06:00", 48) == "source_coverage"
    assert reason(ch6, "2024-03-20 06:00") == "source_coverage"
    assert reason(ch0, "2024-03-20") is None
    assert reason(ch6, "2024-03-21 06:00") == "source_coverage"
    # Lookback [t − 24 h, t) of 03-22 06:00 touches the uncovered 03-21.
    assert reason(ch6, "2024-03-22 06:00") == "lookback_coverage"
    assert reason(ch6, "2024-03-23 06:00") is None
    assert reason(ch6, "2024-03-24 06:00") == "policy_day"
    assert reason(ch0, "2024-03-24") is None
    assert reason(ch6, "2024-03-26 06:00") == "lookback_coverage"
    # Censoring: a window crossing a split end or the data end is excluded, never y=0.
    assert reason(ch6, "2024-12-31 06:00") == "split_boundary"
    assert reason(ch0, "2024-12-31") is None
    assert reason(ch6, "2026-06-30 06:00") == "data_end"
    assert pd.isna(_row(ch6, "2026-06-30 06:00", 2).y_24h)
    assert reason(ch0, "2026-06-30") is None
    assert _row(ch6, "2026-06-29 06:00", 2).split_period == "holdout"


def test_cutoff_0600_truncated_journal_gives_identical_past_state(tmp_path):
    cut = "2024-03-10 06:00:00"
    rows = _rows(1, 10, SMOKE, CH1) + NIGHT
    rows += _rows(
        3,
        10,
        SMOKE,
        [
            ("2024-02-20 10:00:00", OK),
            ("2024-03-10 04:00:00", BAD),  # before the cut, ends after it
            ("2024-03-10 07:00:00", OK),
            ("2024-03-12 10:00:00", BAD),
            ("2024-03-12 11:00:00", OK),
        ],
    )
    # A candidate exactly at the cutoff is not in the past of that cutoff.
    rows += _rows(
        4, 10, SMOKE, [("2024-02-20 10:00:00", OK), (cut, BAD), ("2024-03-10 08:00:00", OK)]
    )
    full, _ = _model(tmp_path, rows, name="full", cutoff_hour=6)
    short, _ = _model(tmp_path, [r for r in rows if r[4] < cut], name="cut", cutoff_hour=6)
    a, b = full("channels"), short("channels")
    keys = ["channel_id", "issued_at"]
    past = keys + [
        "known",
        "candidate",
        "active_episode",
        "in_q_shadow",
        "last_candidate_at",
        "last_start_at",
        "last_end_at",
        "hours_since_last_candidate",
        "candidate_past_7d",
        "window_reason_24h",
    ]
    edge = pd.Timestamp(cut)
    left = a[a.issued_at <= edge].sort_values(keys)[past].reset_index(drop=True)
    right = b[b.issued_at <= edge].sort_values(keys)[past].reset_index(drop=True)
    assert len(left) == 4 * 10
    pd.testing.assert_frame_equal(left, right)
    at_cut = _row(a, cut, 3)
    assert bool(at_cut.active_episode) and pd.isna(at_cut.last_end_at)
    assert pd.isna(_row(a, cut, 4).last_candidate_at)
    # Labels whose window (plus the 1 h duration check) closes before the cut agree.
    last = pd.Timestamp("2024-03-08 06:00")
    cols = keys + ["y_24h", "exclusion_reason_24h"]
    pd.testing.assert_frame_equal(
        a[a.issued_at <= last].sort_values(keys)[cols].reset_index(drop=True),
        b[b.issued_at <= last].sort_values(keys)[cols].reset_index(drop=True),
    )


def test_cutoff_hour_zero_is_the_midnight_protocol_and_config_sets_it(tmp_path):
    rows = _rows(1, 10, SMOKE, CH1) + NIGHT
    default, _ = _model(tmp_path, rows, name="default")
    explicit, _ = _model(tmp_path, rows, name="explicit", cutoff_hour=0)
    for kind, horizon in (("channels", None), ("cards", 24), ("links", 24)):
        pd.testing.assert_frame_equal(default(kind, horizon), explicit(kind, horizon))
    model = json.loads((DATA_SCIENCE / V2).read_text(encoding="utf-8"))
    from_config, manifests = _model(
        tmp_path,
        rows,
        name="config",
        target="connection_loss_gt2s",
        model_config={**model, "cutoff_hour": 6},
    )
    assert set(from_config("channels").issued_at.dt.hour) == {6}
    assert manifests[0]["cutoff_hour"] == 6
    for bad in (24, -1, 6.5):
        with pytest.raises(ValueError, match="cutoff_hour"):
            check_cutoff_hour(bad)

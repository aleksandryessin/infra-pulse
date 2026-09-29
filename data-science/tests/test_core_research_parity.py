"""The core phase detector and list equal the v9 research code on synthetic data.

Real-data parity is ``scripts/check_core_parity_v9.py`` (aggregated report in
``reports/core-research-parity-v9-2026-09-27.json``); here random synthetic journals
exercise the same definitions without private data.
"""

import random
from datetime import datetime, timedelta

import duckdb
import numpy as np
import pandas as pd
import pytest

from infra_pulse_core.features import incident_list as il
from infra_pulse_core.features.phase_feeder_episodes import (
    MSK,
    PhaseEvent,
    Record,
    cluster_events,
    detect,
)
from infra_pulse_research.modeling import sensor_failure_evaluation as ev
from infra_pulse_research.modeling import sensor_failure_release as rel
from infra_pulse_research.modeling.sensor_failure_target import (
    load_config,
    materialize_failure_tables,
    provisional_technical_sql,
)
from infra_pulse_research.modeling.subsystem_grid import materialize_registries

PHASE = "Состояние фазы"
STATES = ["Неисправен", "Обесточен", "Есть питание", "Норма", "Неопределен", "Выключен"]


def _journal(seed: int, channels: int = 6, rows: int = 900) -> list[tuple]:
    rnd = random.Random(seed)
    start = datetime(2024, 1, 1)
    out = []
    for channel in range(1, channels + 1):
        at = start
        for _ in range(rows // channels):
            at += timedelta(minutes=rnd.choice([0, 1, 5, 30, 240, 600, 1440, 2000]))
            value = rnd.choices(STATES, weights=[3, 3, 4, 2, 2, 1])[0]
            out.append((channel, 10 + channel % 2, value, at))
            if rnd.random() < 0.05:  # a second record in the same second (never ordered)
                out.append((channel, 10 + channel % 2, rnd.choice(STATES), at))
    return out


def _research(tmp_path, journal):
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE current_enriched_events (
          channel_id BIGINT, object_id BIGINT, system_type VARCHAR, sensor_type VARCHAR,
          event_ts_local_raw VARCHAR, value_raw VARCHAR, value_numeric DOUBLE, alarm BOOLEAN,
          is_epoch_placeholder BOOLEAN, sensor_name_current VARCHAR, object_kind VARCHAR,
          picket_from DOUBLE, picket_to DOUBLE, picket_form VARCHAR, picket_parsed BOOLEAN
        )
    """)
    con.executemany(
        "INSERT INTO current_enriched_events VALUES (?, ?, 'Диспетчерский контроль', ?, ?, ?,"
        " NULL, ?, false, 'n', 'k', NULL, NULL, NULL, false)",
        [
            [channel, obj, PHASE, at.strftime("%Y-%m-%d %H:%M:%S"), value, value == "Неисправен"]
            for channel, obj, value, at in journal
        ],
    )
    con.execute("""
        CREATE VIEW coverage_days AS SELECT CAST(day AS DATE) AS day, true AS working_covered
        FROM generate_series(DATE '2023-12-01', DATE '2026-06-30', INTERVAL 1 DAY) t(day)
    """)
    con.execute("CREATE TABLE policy_days (day DATE, sensor_type_scope VARCHAR)")
    materialize_registries(con, tmp_path / "registry")
    files = materialize_failure_tables(
        con, tmp_path / "tables", config=load_config(), technical_sql=provisional_technical_sql
    )
    frame = pd.read_parquet(files["episodes_q24h"])
    return frame[frame.label == "connection_loss"]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_core_episodes_equal_research_sql(tmp_path, seed):
    journal = _journal(seed)
    research = _research(tmp_path, journal)
    episodes, _ = detect(
        Record(str(channel), at.replace(tzinfo=MSK), value, value == "Неисправен", str(obj))
        for channel, obj, value, at in journal
    )
    core = {(e.channel_id, e.start_at.replace(tzinfo=None)): e.end_at for e in episodes}
    expected = {
        (str(row.channel_id), row.start_at.to_pydatetime()): row.end_at
        for row in research.itertuples()
    }
    assert set(core) == set(expected)
    for key, end in expected.items():
        got = core[key]
        assert (None if pd.isna(end) else end.to_pydatetime()) == (
            None if got is None else got.replace(tzinfo=None)
        )
    # Events: W = 10 min chains of the object's starts (LABELS.md).
    research_events = set(
        research.groupby(["object_id", "cluster_id_w10"])["start_at"]
        .min()
        .reset_index()
        .apply(lambda r: (str(r.object_id), r.start_at.to_pydatetime()), axis=1)
    )
    core_events = {(e.object_id, e.start_at.replace(tzinfo=None)) for e in cluster_events(episodes)}
    assert core_events == research_events


def _cards_and_members(seed: int, pairs: int = 6, days: int = 120):
    rnd = np.random.default_rng(seed)
    start = pd.Timestamp("2024-01-01")
    members = []
    for pair in range(pairs):
        for k, day in enumerate(sorted(rnd.choice(days + 300, size=rnd.integers(5, 60)))):
            at = start - pd.Timedelta(days=300) + pd.Timedelta(days=int(day), hours=int(k % 24))
            members.append(
                {
                    "event_id": f"e{pair}-{k}",
                    "object_id": pair,
                    "sensor_type": PHASE,
                    "start_at": at,
                    "qualifying": True,
                }
            )
    members = pd.DataFrame(members)
    cards = pd.DataFrame(
        [
            {
                "object_id": pair,
                "sensor_type": PHASE,
                "issued_at": start + pd.Timedelta(days=d),
                "exclusion_reason": None,
                "candidate_channels": 1.0,
            }
            for d in range(days)
            for pair in range(pairs)
        ]
    )
    return cards, members


@pytest.mark.parametrize("seed", [4, 5])
def test_static_list_and_rolling_selection_equal_research(seed):
    cards, members = _cards_and_members(seed)
    research_score = ev.static_list_scores(cards, members, None).to_numpy()
    events = [
        PhaseEvent(
            row.event_id,
            str(row.object_id),
            row.start_at.to_pydatetime().replace(tzinfo=MSK),
            row.start_at.to_pydatetime().replace(tzinfo=MSK),
            ("c",),
            (row.start_at.to_pydatetime().replace(tzinfo=MSK),),
        )
        for row in members.itertuples()
    ]
    index = il.EventIndex(events)
    core_score = [
        index.score(str(row.object_id), row.issued_at.to_pydatetime().replace(tzinfo=MSK))
        for row in cards.itertuples()
    ]
    assert list(research_score) == core_score
    # Release: the first event of the pair in [t, t + 14 d).
    starts = members.groupby("object_id")["start_at"].apply(sorted).to_dict()
    first = []
    for row in cards.itertuples():
        inside = [
            s
            for s in starts[row.object_id]
            if row.issued_at <= s < row.issued_at + pd.Timedelta(days=14)
        ]
        first.append(inside[0] if inside else pd.NaT)
    cards["static_list"] = research_score
    cards["first_event_start"] = first
    rng = np.random.default_rng(seed)
    eligible = rng.random(len(cards)) > 0.1
    mask = rel.select_rolling(cards, "static_list", k=3, days=14, eligible=eligible)
    issued: list[il.IssuedCard] = []
    core_mask = np.zeros(len(cards), dtype=bool)
    for day, group in cards.groupby("issued_at", sort=True):
        t = day.to_pydatetime().replace(tzinfo=MSK)
        pairs = [
            il.PairAtCutoff(
                str(row.object_id), int(row.static_list), bool(eligible[pos]), frozenset({"c"})
            )
            for pos, row in zip(group.index, group.itertuples(), strict=True)
        ]
        for pair, _rank, _ranked in il.select_new(t, pairs, issued, k=3, days=14):
            pos = group.index[group["object_id"] == int(pair.object_id)][0]
            core_mask[pos] = True
            release = cards.loc[pos, "first_event_start"]
            issued.append(
                il.IssuedCard(
                    pair.object_id,
                    t,
                    None if pd.isna(release) else release.to_pydatetime().replace(tzinfo=MSK),
                )
            )
    assert (mask == core_mask).all()


def test_incident_heads_equal_research():
    rnd = random.Random(8)
    at = datetime(2024, 1, 1)
    starts = []
    for _ in range(80):
        at += timedelta(hours=rnd.choice([1, 5, 23, 24, 25, 100, 400]))
        starts.append(at)
    pairs = pd.DataFrame(
        {
            "event_id": [f"e{i}" for i in range(len(starts))],
            "object_id": 1,
            "sensor_type": PHASE,
            "event_start": starts,
        }
    )
    research = rel.incidents(pairs).sort_values("event_start")
    heads = il.incident_heads(starts)
    assert research["incident_first"].tolist() == heads
    fresh = [il.fresh_incident(starts, i) for i, head in enumerate(heads) if head]
    assert research.loc[research["incident_first"], "incident_fresh"].tolist() == fresh

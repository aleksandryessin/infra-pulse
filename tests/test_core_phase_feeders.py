"""Core phase detector, list and channel names on synthetic data only."""

import json
import random
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

from infra_pulse_core.contracts.forecast import (
    FEEDER_TARGET,
    TARGET_LABELS,
    ForecastCard,
    ForecastChannel,
    ForecastFact,
    ForecastFreshness,
    ForecastJournalEntry,
    ForecastOutcome,
    ForecastScore,
    ForecastVersions,
    FrequencyBucket,
)
from infra_pulse_core.features import incident_list as il
from infra_pulse_core.features.channel_names import (
    classify_channel,
    feeder_kind,
    name_stem,
    parse_picket,
)
from infra_pulse_core.features.phase_feeder_episodes import (
    MSK,
    ChannelState,
    PhaseEpisodeDetector,
    Record,
    cluster_events,
    detect,
)

T0 = datetime(2026, 3, 1, tzinfo=MSK)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def rec(channel: str, hours: float, value: str, obj: str = "1") -> Record:
    return Record(channel, at(hours), value, value == "Неисправен", obj)


# -- channel names -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "form", "low", "high"),
    [
        ("РО2 ПК28", "point", 28, None),
        ("ГРО10 ПК96-81 (каб.)", "range", 81, 96),
        ("ГРО20 ПК451-ПК469", "range", 451, 469),
        ("ГРО2 ПК24(ПК9-ПК24)", "point", 24, None),
        ("ФВ1 ПК12,5", "point", 12.5, None),
        ("ФРО2 (ГРО38-41)", "unknown", None, None),
        ("Группа ГРО16 ПК195-209\r\n", "range", 195, 209),
    ],
)
def test_picket_from_name(name, form, low, high):
    picket = parse_picket(name)
    assert (picket.form, picket.picket_from, picket.picket_to) == (form, low, high)
    assert picket.basis == (None if form == "unknown" else "channel_name")


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("ГРО5 ПК75", "lighting"),
        ("13РО ПК827-852", "lighting"),
        ("ФАО1 ПК34 Э/щ 3", "lighting"),
        ("Фидер ФРО1 (ГРО11-17)", "lighting"),
        ("Фидер ФВ9 (В38)", "ventilation"),
        ("ФАНС1 (АНС5 ПК344)", "pumps"),
        ("ОЗК2 закр. ПК730", "ozk"),
        ("ФОЗК1 ПК3", "ozk"),
        ("ФТС2 ПК399", "other"),
        ("Фрез.1 ПК0", "other"),
        ("ФВР1 ПК10", "other"),
    ],
)
def test_feeder_kind_dictionary(name, kind):
    assert feeder_kind(name) == kind
    assert classify_channel(name).role == "feeder"


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("АВР Ввод2 ПК300", "ats"),
        ("ЩАП-23 ПК168 ДП щит Ввод2", "panel"),
        ("Ввод1 ПК10", "input"),
        ("Межсекционный ПК5", "other"),
    ],
)
def test_landmarks(name, kind):
    layout = classify_channel(name)
    assert (layout.role, layout.landmark_kind, layout.feeder_kind) == ("landmark", kind, None)


def test_name_stem_masks_numbers():
    assert name_stem("ФТС2  ПК244 щит.3") == "ФТС# ПК# щит.#"


# -- detector ----------------------------------------------------------------------------------


def test_start_end_q_shadow_stuck_channel_and_conflict():
    records = [
        rec("a", 0, "Есть питание"),
        rec("a", 1, "Неисправен"),  # start
        rec("a", 1.1, "Неопределен"),  # neutral
        rec("a", 2, "Обесточен"),  # end (clean)
        rec("a", 10, "Неисправен"),  # < Q after the previous candidate: no start
        rec("a", 11, "Есть питание"),
        rec("a", 40, "Неисправен"),  # >= Q and clean since: start
        rec("a", 90, "Неисправен"),  # stuck: no clean since the last candidate
        rec("a", 91, "Есть питание"),
        rec("a", 130, "Неисправен"),  # start with a clean record in the same second
        rec("a", 130, "Есть питание"),
    ]
    episodes, candidates = detect(records)
    assert [(e.start_at, e.end_at) for e in episodes] == [
        (at(1), at(2)),
        (at(40), at(91)),
        (at(130), None),
    ]
    assert episodes[0].power_off_at is None  # «Обесточен» 1 h after the start, not within 10 min
    assert len(candidates) == 5


def test_power_off_followup_within_ten_minutes():
    records = [
        rec("a", 0, "Есть питание"),
        rec("a", 1, "Неисправен"),
        rec("a", 1 + 5 / 60, "Обесточен"),
        rec("b", 0, "Есть питание"),
        rec("b", 1, "Неисправен"),
        rec("b", 1.5, "Обесточен"),
    ]
    episodes = {e.channel_id: e for e in detect(records)[0]}
    assert episodes["a"].power_off_at == at(1 + 5 / 60)
    assert episodes["b"].power_off_at is None


def _random_records(seed: int, channels: int = 4, count: int = 600) -> list[Record]:
    rnd = random.Random(seed)
    states = ["Неисправен", "Обесточен", "Есть питание", "Неопределен", "Норма"]
    out = []
    for channel in range(channels):
        hours = 0.0
        for _ in range(count // channels):
            hours += rnd.choice([0, 0.01, 0.2, 3, 10, 20, 30])
            out.append(rec(f"c{channel}", hours, rnd.choice(states), str(channel % 2)))
    return out


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_incremental_parts_equal_whole_run(seed):
    records = _random_records(seed)
    whole, whole_candidates = detect(records)
    ordered = sorted(records, key=lambda r: r.event_at)
    rnd = random.Random(seed)
    cuts = sorted(rnd.sample(range(1, len(ordered)), 6))
    detector = PhaseEpisodeDetector()
    episodes, candidates = {}, set()
    for low, high in zip([0, *cuts], [*cuts, len(ordered)], strict=True):
        # Persist and reload the state between uploads, as the worker does.
        detector = PhaseEpisodeDetector.from_state_json(
            json.loads(json.dumps(detector.state_json()))
        )
        delta = detector.process(ordered[low:high])
        assert not delta.late_channels
        episodes.update(delta.episodes)
        candidates |= delta.candidates
    assert sorted(episodes.values(), key=lambda e: (e.start_at, e.channel_id)) == whole
    assert candidates == whole_candidates


def test_same_second_split_across_uploads_is_merged():
    first = [rec("a", 0, "Есть питание"), rec("a", 1, "Неисправен"), rec("a", 30, "Норма")]
    detector = PhaseEpisodeDetector()
    delta = detector.process(first)
    assert delta.episodes[("a", at(1))].end_at == at(30)
    # The same second arrives later with «Неисправен»: the end is retracted.
    delta = detector.process([rec("a", 30, "Неисправен")])
    assert delta.episodes[("a", at(1))].end_at is None
    whole, _ = detect([*first, rec("a", 30, "Неисправен")])
    assert whole[0].end_at is None


def test_late_record_asks_for_a_rebuild():
    detector = PhaseEpisodeDetector()
    detector.process([rec("a", 5, "Норма")])
    delta = detector.process([rec("a", 1, "Неисправен"), rec("b", 1, "Неисправен")])
    assert delta.late_channels == {"a"}
    assert ("b", at(1)) in delta.episodes


def test_state_json_round_trip():
    detector = PhaseEpisodeDetector()
    detector.process(_random_records(7))
    for channel, state in detector.states.items():
        restored = ChannelState.from_json(json.loads(json.dumps(state.to_json())))
        assert restored.to_json() == state.to_json(), channel


def test_events_chain_starts_of_an_object_within_ten_minutes():
    records = [
        rec("a", 0, "Неисправен"),
        rec("b", 0.1, "Неисправен"),  # 6 min later: same event
        rec("c", 0.3, "Неисправен"),  # 12 min after b: new event
        rec("d", 0.1, "Неисправен", obj="2"),
    ]
    events = cluster_events(detect(records)[0])
    assert [(e.object_id, e.size) for e in events] == [("1", 2), ("2", 1), ("1", 1)]


# -- list --------------------------------------------------------------------------------------


def test_channel_at_cutoff_known_active_and_q_shadow():
    t = at(48)
    fresh = il.channel_at_cutoff(
        "a", t, first_seen=at(47), last_candidate_before=at(40), active_episode=False
    )
    assert fresh.known and fresh.q_shadow and not fresh.candidate
    later = il.channel_at_cutoff(
        "a", t, first_seen=at(48), last_candidate_before=None, active_episode=False
    )
    assert not later.known


def test_rolling_list_holds_until_release_and_refills_next_cutoff():
    t = il.cutoff_of(T0.date())
    pairs = [il.PairAtCutoff(str(o), 10 - o, True, frozenset({"x"})) for o in range(1, 5)]
    chosen = il.select_new(t, pairs, [], k=2)
    assert [(pair.object_id, rank) for pair, rank, _ in chosen] == [("1", 1), ("2", 2)]
    # Object 1 released at 10:00 the next day, object 2 released exactly at the next cutoff.
    issued = [
        il.IssuedCard("1", t, t + timedelta(days=1, hours=10)),
        il.IssuedCard("2", t, t + timedelta(days=1)),
    ]
    next_day = t + timedelta(days=1)
    assert il.select_new(next_day, pairs, issued, k=2) == []  # an event at 00:00 keeps the card
    after = il.select_new(t + timedelta(days=2), pairs, issued, k=2)
    assert [pair.object_id for pair, _, _ in after] == ["1", "2"]
    # Ineligible pairs never take a place; ties go by the numeric object ID.
    ties = [
        il.PairAtCutoff("10", 5, True, frozenset()),
        il.PairAtCutoff("9", 5, True, frozenset()),
        il.PairAtCutoff("1", 9, False, frozenset()),
    ]
    assert [p.object_id for p, _, _ in il.select_new(t, ties, [], k=3)] == ["9", "10"]


@pytest.mark.parametrize("events", [0, 37, 38, 62, 63, 500])
def test_frequency_bins_contain_their_share(events):
    row = il.frequency_row(events)
    low, high = row.interval
    assert low <= row.share <= high
    assert row.level in ("medium", "high")


def test_recurrence_and_score_are_point_in_time():
    t = il.cutoff_of(T0.date())
    from infra_pulse_core.features.phase_feeder_episodes import PhaseEvent

    events = il.EventIndex(
        [
            PhaseEvent(
                f"e{i}",
                "1",
                t - timedelta(days=d),
                t - timedelta(days=d),
                ("a",),
                (t - timedelta(days=d),),
            )
            for i, d in enumerate([3, 20, 400])
        ]
        + [PhaseEvent("future", "1", t, t, ("a",), (t,))]
    )
    assert events.score("1", t) == 2
    assert events.recurrence("1", t) == "chronic"
    assert events.recurrence("1", t - timedelta(days=4)) == "fresh"
    first = events.first_event("1", t, {"a"})
    assert first is not None and first.event_id == "future"
    assert events.first_event("1", t, {"b"}) is None


def test_incident_heads_and_fresh():
    starts = [at(0), at(5), at(40), at(400)]
    assert il.incident_heads(starts) == [True, False, True, True]
    assert [il.fresh_incident(starts, i) for i in (0, 2, 3)] == [True, False, True]


# -- contract: a top-5 card realizes on an unlisted candidate feeder ---------------------------


def _card(channels_total: int) -> ForecastCard:
    t = il.cutoff_of(T0.date())
    year = t - timedelta(days=365)
    fact = ForecastFact(
        kind="events_365d",
        text="Эпизодов за 365 сут: 3",
        period_start=year,
        period_end=t,
        value_number=3,
    )
    row = il.frequency_row(40)
    low, high = row.interval
    return ForecastCard(
        id="synthetic-card",
        mode="received",
        target_spec_id=FEEDER_TARGET,
        target_label=TARGET_LABELS[FEEDER_TARGET],
        object_id="1",
        sensor_type="Состояние фазы",
        horizon="336h",
        issued_at=t,
        window_start=t,
        window_end=t + timedelta(days=14),
        published_at=t,
        freshness=ForecastFreshness(data_as_of=t, lookback_days=365, coverage="complete"),
        status="scored",
        score=ForecastScore(
            kind="frequency_share",
            label="доля k из n",
            value=row.share,
            rank=1,
            ranked_cards=5,
            card_budget=10,
            frequency=FrequencyBucket(
                table_version="v",
                events_from=row.events_from,
                events_to=row.events_to,
                cards=row.cards,
                positive_cards=row.positive_cards,
                share=row.share,
                wilson_low=low,
                wilson_high=high,
                period_start=il.FREQUENCY_PERIOD[0],
                period_end=il.FREQUENCY_PERIOD[1],
                level=row.level,
                source_report="synthetic",
            ),
        ),
        risk_level=row.level,
        channels=[
            ForecastChannel(
                channel_id=f"f{i}", rank_in_card=i, picket_form="unknown", reason_facts=[fact]
            )
            for i in range(1, 6)
        ],
        channels_total=channels_total,
        versions=ForecastVersions(
            scorer="static_list",
            model_version="m",
            feature_version="f",
            label_version="l",
            policy_version="p",
            release_id="r",
            run_id="run",
        ),
    )


def _realized_on(card: ForecastCard, channel: str) -> ForecastJournalEntry:
    event = card.issued_at + timedelta(hours=30)
    return ForecastJournalEntry(
        journal_position=1,
        card=card,
        outcome=ForecastOutcome(
            status="realized",
            label_version="l",
            resolved_at=event,
            first_event_at=event,
            lead_hours=30.0,
            event_channel_ids=[channel],
            event_cluster_size=1,
        ),
        list_state="released",
        released_at=event,
    )


def test_top_five_card_realizes_on_an_unlisted_feeder():
    entry = _realized_on(_card(channels_total=27), "f-unlisted")
    assert entry.outcome.status == "realized"
    with pytest.raises(ValueError, match="candidate channels"):
        _realized_on(_card(channels_total=5), "f-unlisted")


def test_http_forecast_reads_do_not_import_numpy_pandas_or_research():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'infra_pulse_research', 'mlflow', 'numpy', 'pandas',
                                      'duckdb', 'pyarrow', 'sklearn', 'catboost'}:
            raise AssertionError('HTTP imported ' + fullname)
sys.meta_path.insert(0, Block())
from infra_pulse_backend.api import forecast_db
from infra_pulse_backend.operations import forecast_publish
from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings
create_app(Settings(mode='received', received_stream_id='s', _env_file=None))
""",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

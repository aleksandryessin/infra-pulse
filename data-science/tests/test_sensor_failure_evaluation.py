"""Synthetic checks for sensor-failure card/event evaluation."""

import json

import numpy as np
import pandas as pd
import pytest

from infra_pulse_research.modeling.sensor_failure_evaluation import (
    attach_card_scores,
    bootstrap_intervals,
    bootstrap_variant_deltas,
    ceilings,
    evaluate_cards,
    evaluate_policy,
    event_day_flags,
    event_outcomes,
    fit_calibrator,
    select_cards,
    select_policy,
    static_list_scores,
    to_unit_b,
)

SMOKE = "Датчик дыма"
GAS = "Газовый датчик"
T1 = pd.Timestamp("2025-08-04")
T2 = pd.Timestamp("2025-08-05")


def _card(obj, sensor, day, outcome="negative", reason=None, split="holdout", **extra):
    row = {
        "object_id": obj,
        "sensor_type": sensor,
        "system_type": "Пожарная охрана" if sensor == SMOKE else "Газовая охрана",
        "issued_at": day,
        "split_period": split,
        "outcome": outcome,
        "y_card": {"positive": 1, "negative": 0}.get(outcome, np.nan),
        "exclusion_reason": reason,
        "object_failing_past_24h": False,
        "weekend": day.dayofweek >= 5,
    }
    row.update(extra)
    return row


def test_card_score_is_max_over_candidate_channels_only():
    cards = pd.DataFrame(
        [_card(1, SMOKE, T1), _card(2, SMOKE, T1, "excluded", "no_candidate_channels")]
    )
    channels = pd.DataFrame(
        {
            "object_id": [1, 1, 1, 2],
            "sensor_type": [SMOKE] * 4,
            "issued_at": [T1] * 4,
            "candidate": [True, True, False, False],
            "score": [0.2, 0.4, 0.99, 0.9],
        }
    )
    scored = attach_card_scores(cards, channels, ["score"])
    assert scored.score.iloc[0] == 0.4
    assert np.isnan(scored.score.iloc[1])


def test_selection_budget_tie_break_and_excluded_cards():
    cards = pd.DataFrame(
        [
            _card(3, SMOKE, T1, score=0.5),
            _card(1, SMOKE, T1, score=0.5),
            _card(2, GAS, T1, score=0.9, outcome="excluded", reason="source_coverage"),
            _card(1, GAS, T1, score=0.1),
        ]
    )
    assert select_cards(cards, "score", budget=1).tolist() == [False, True, False, False]
    assert select_cards(cards, "score", budget=5).tolist() == [True, True, False, True]


def _event_setup():
    cards = pd.DataFrame(
        [
            _card(1, SMOKE, T1, "positive", score=0.9),
            _card(1, GAS, T1, "negative", score=0.1),
            _card(2, SMOKE, T1, "excluded", "no_candidate_channels", score=np.nan),
            _card(3, SMOKE, T1, "excluded", "source_coverage", score=np.nan),
            _card(4, SMOKE, T2, "positive", score=0.2),
        ]
    )
    events = pd.DataFrame(
        {
            "event_id": ["a", "b", "c", "d", "e"],
            "object_id": [1, 2, 3, 4, 1],
            "event_start": [
                T1 + pd.Timedelta(hours=5),
                T1 + pd.Timedelta(hours=1),
                T1 + pd.Timedelta(hours=2),
                T2 + pd.Timedelta(hours=3),
                T1 + pd.Timedelta(hours=7),
            ],
            "size": [3, 1, 1, 1, 1],
            "sensor_types": [[SMOKE, GAS], [SMOKE], [SMOKE], [SMOKE], [SMOKE]],
            "system_types": [["Пожарная охрана"]] * 5,
            "qualifies": [True, True, True, True, False],
        }
    )
    links = pd.DataFrame(
        {
            "object_id": [1, 1, 2, 3, 4, 1],
            "sensor_type": [SMOKE, GAS, SMOKE, SMOKE, SMOKE, SMOKE],
            "issued_at": [T1, T1, T1, T1, T2, T1],
            "event_id": ["a", "a", "b", "c", "d", "e"],
            "candidate_member": [True, True, False, True, True, True],
        }
    )
    return cards, links, events


def test_event_capture_lead_and_denominators():
    cards, links, events = _event_setup()
    selected = select_cards(cards, "score", budget=1)
    ev = event_outcomes(cards, links, events, selected).set_index("event_id")
    assert "e" not in ev.index  # not qualifying
    assert ev.loc["a"].captured and ev.loc["a"].lead_hours == 5.0
    assert ev.loc["b"].evaluable and not ev.loc["b"].capturable
    assert not ev.loc["c"].evaluable
    assert ev.loc["d"].captured and ev.loc["d"].lead_hours == 3.0
    result = evaluate_cards(cards, links, events, "score", budget=1)
    json.dumps(result)
    assert result["events"]["events"] == 3
    assert result["events"]["recall"] == pytest.approx(2 / 3)
    assert result["events"]["recall_capturable"] == 1.0
    assert result["card_precision"] == 1.0 and result["cards_selected"] == 2
    assert result["events_mass"]["events"] == 1
    assert result["events_by_sensor_type"][GAS]["captured"] == 1
    assert "object_id" not in json.dumps(result)


def test_calibration_is_fitted_only_on_calibration_period():
    rng = np.random.default_rng(0)
    rows = []
    for i in range(200):
        s = float(rng.random())
        outcome = "positive" if rng.random() < s else "negative"
        rows.append(_card(i, SMOKE, T1, outcome, split="calibration_and_threshold", score=s))
    calib = pd.DataFrame(rows)
    model = fit_calibrator(calib, "score")
    mixed = pd.concat([calib, pd.DataFrame([_card(999, SMOKE, T2, "positive", score=0.1)])])
    with pytest.raises(ValueError, match="calibration_and_threshold"):
        fit_calibrator(mixed, "score")
    cards, links, events = _event_setup()
    result = evaluate_cards(cards, links, events, "score", budget=1, calibrator=model)
    assert 0 <= result["calibration"]["ece"] <= 1
    # The holdout frame does not feed the fitted map.
    assert np.allclose(
        model.predict([0.1, 0.5, 0.9]), fit_calibrator(calib, "score").predict([0.1, 0.5, 0.9])
    )


def test_bootstrap_reproducible_and_paired():
    rng = np.random.default_rng(3)
    cards, links, events = [], [], []
    for d in pd.date_range("2025-08-04", periods=21):
        for obj in range(8):
            good = float(rng.random())
            outcome = "positive" if rng.random() < 0.1 + 0.6 * good else "negative"
            cards.append(_card(obj, SMOKE, d, outcome, good=good, noise=float(rng.random())))
            if outcome == "positive":
                eid = f"{obj}-{d.date()}"
                events.append(
                    {
                        "event_id": eid,
                        "object_id": obj,
                        "event_start": d + pd.Timedelta(hours=4),
                        "size": 1,
                        "sensor_types": [SMOKE],
                        "system_types": ["Пожарная охрана"],
                        "qualifies": True,
                    }
                )
                links.append(
                    {
                        "object_id": obj,
                        "sensor_type": SMOKE,
                        "issued_at": d,
                        "event_id": eid,
                        "candidate_member": True,
                    }
                )
    cards, links, events = pd.DataFrame(cards), pd.DataFrame(links), pd.DataFrame(events)
    kwargs = {"budget": 3, "replicates": 60, "deltas": [("good", "noise", "signal")]}
    first = bootstrap_intervals(cards, links, events, ["good", "noise"], seed=17, **kwargs)
    assert first == bootstrap_intervals(cards, links, events, ["good", "noise"], seed=17, **kwargs)
    assert first != bootstrap_intervals(cards, links, events, ["good", "noise"], seed=5, **kwargs)
    json.dumps(first)
    week = first["schemes"]["iso_week"]
    assert week["blocks"] == 3 and first["schemes"]["object_id"]["blocks"] == 8
    point = evaluate_cards(cards, links, events, "good", budget=3)
    assert week["scores"]["good"]["event_recall"]["point"] == pytest.approx(
        point["events"]["recall"]
    )
    delta = week["deltas"]["signal"]["event_recall"]
    assert delta["point"] == pytest.approx(
        week["scores"]["good"]["event_recall"]["point"]
        - week["scores"]["noise"]["event_recall"]["point"]
    )


def _day(n):
    return pd.Timestamp("2024-03-04") + pd.Timedelta(days=n)


def test_rolling_policy_keeps_open_cards_and_frees_slots():
    # (day, object, score); all cards evaluable
    spec = [
        (0, 1, 0.9),
        (0, 2, 0.8),
        (0, 3, 0.1),
        (1, 1, 0.99),
        (1, 4, 0.95),
        (1, 2, 0.5),
        (2, 4, 0.9),
        (2, 5, 0.2),
        (3, 1, 0.65),
        (3, 4, 0.7),
        (3, 6, 0.6),
        (4, 7, 0.1),
    ]
    cards = pd.DataFrame([_card(o, SMOKE, _day(d), score=sc) for d, o, sc in spec])
    mask = select_policy(cards, "score", {"type": "rolling", "max_open": 2, "window_days": 3})
    issued = [(d, o) for (d, o, _), m in zip(spec, mask, strict=True) if m]
    # Day 0 fills both slots; days 1-2 cannot displace them; they close on day 3.
    assert issued == [(0, 1), (0, 2), (3, 1), (3, 4)]
    # One card per unit per open window: object 1 is reissued only after it closed.
    for max_open in (1, 2, 3):
        m = select_policy(
            cards, "score", {"type": "rolling", "max_open": max_open, "window_days": 3}
        )
        issued_days = cards.loc[m, "issued_at"]
        for day in cards.issued_at.unique():
            open_now = ((issued_days <= day) & (issued_days > day - pd.Timedelta(days=3))).sum()
            assert open_now <= max_open


def test_cadence_and_topk_policies():
    cards = pd.DataFrame(
        [_card(o, SMOKE, _day(d), score=float(o)) for d in range(4) for o in range(1, 6)]
    )
    cadence = select_policy(cards, "score", {"type": "cadence", "every_days": 2, "k": 2})
    assert sorted(set(cards.loc[cadence, "issued_at"])) == [_day(0), _day(2)]
    assert cadence.sum() == 4
    assert select_policy(cards, "score", {"type": "topk", "k": 1}).sum() == 4


def test_unit_b_groups_by_system_except_dispatch():
    disp = "Диспетчерский контроль"
    cards = pd.DataFrame(
        [
            _card(1, SMOKE, T1, "negative", score=0.2),
            _card(1, "Тепловой датчик", T1, "positive", score=0.7, system_type="Пожарная охрана"),
            _card(1, "Состояние фазы", T1, "positive", score=0.4, system_type=disp),
            _card(1, "Состояние насоса", T1, "negative", score=0.9, system_type=disp),
            _card(1, GAS, T1, "excluded", "no_candidate_channels", score=np.nan),
        ]
    )
    links = pd.DataFrame(
        {
            "object_id": [1, 1],
            "sensor_type": ["Тепловой датчик", "Состояние фазы"],
            "issued_at": [T1, T1],
            "event_id": ["x", "y"],
            "event_start": [T1 + pd.Timedelta(hours=2)] * 2,
            "candidate_member": [True, True],
        }
    )
    unit_b, links_b = to_unit_b(cards, links, ["score"])
    by = unit_b.set_index("sensor_type")
    assert set(by.index) == {
        "Пожарная охрана",
        "Состояние фазы",
        "Состояние насоса",
        "Газовая охрана",
    }
    fire = by.loc["Пожарная охрана"]
    assert (fire.y_card, fire.score, fire.member_cards) == (1, 0.7, 2)
    assert by.loc["Газовая охрана"].exclusion_reason == "no_candidate_channels"
    assert by.loc["Состояние насоса"].y_card == 0
    assert sorted(links_b.sensor_type) == ["Пожарная охрана", "Состояние фазы"]
    events = pd.DataFrame(
        {
            "event_id": ["x", "y"],
            "object_id": [1, 1],
            "event_start": [T1 + pd.Timedelta(hours=2)] * 2,
            "size": [1, 1],
            "sensor_types": [["Тепловой датчик"], ["Состояние фазы"]],
            "system_types": [["Пожарная охрана"], [disp]],
            "qualifies": [True, True],
        }
    )
    result = evaluate_policy(unit_b, links_b, events, "score", {"type": "topk", "k": 1})
    # The pump unit has the top score but is negative; the fire group is not issued.
    assert (result["card_precision"], result["events"]["recall"]) == (0.0, 0.0)
    ceiling = ceilings(unit_b, links_b, events, {"type": "topk", "k": 1})
    assert (ceiling["card_precision"], ceiling["event_recall"]) == (1.0, 0.5)


def test_ceilings_equal_perfect_ranking():
    cards, links, events = _event_setup()
    policy = {"type": "topk", "k": 1}
    cap = ceilings(cards, links, events, policy)
    perfect = cards.assign(perfect=cards["y_card"].fillna(0))
    manual = evaluate_policy(perfect, links, events, "perfect", policy)
    assert cap["card_precision"] == manual["card_precision"] == 1.0
    assert cap["event_recall"] == manual["events"]["recall"]


def _week_cards(n_objects=5, days=7, start="2024-03-04"):
    """Monday..Sunday cutoffs; score = object number (higher first)."""
    return pd.DataFrame(
        [
            _card(o, SMOKE, pd.Timestamp(start) + pd.Timedelta(days=d), score=float(o))
            for d in range(days)
            for o in range(1, n_objects + 1)
        ]
    )


def test_calendar_budget_policy_weekdays_weekends_and_verified_holidays():
    cards = _week_cards()
    policy = {"type": "topk", "k": 2, "weekend_k": 1}
    mask = select_policy(cards, "score", policy)
    per_day = cards[mask].groupby("issued_at").size()
    assert per_day.tolist() == [2, 2, 2, 2, 2, 1, 1]
    # Highest scores are kept on a reduced day as well.
    assert cards[mask & (cards.issued_at == _day(5))].object_id.tolist() == [5]
    # Holidays apply only from a supplied (verified) calendar.
    with_holiday = select_policy(cards, "score", {**policy, "holidays": ["2024-03-06"]})
    assert cards[with_holiday].groupby("issued_at").size().tolist() == [2, 2, 1, 2, 2, 1, 1]
    none_on_weekends = select_policy(cards, "score", {**policy, "weekend_k": 0})
    assert cards[none_on_weekends].issued_at.dt.dayofweek.max() == 4
    assert select_policy(cards, "score", {"type": "topk", "k": 2}).sum() == 14
    events = pd.DataFrame(
        {
            "event_id": ["e"],
            "object_id": [5],
            "event_start": [_day(0) + pd.Timedelta(hours=10)],
            "size": [1],
            "sensor_types": [[SMOKE]],
            "system_types": [["Пожарная охрана"]],
            "qualifies": [True],
        }
    )
    links = pd.DataFrame(
        {
            "object_id": [5],
            "sensor_type": [SMOKE],
            "issued_at": [_day(0)],
            "event_id": ["e"],
            "candidate_member": [True],
        }
    )
    load = evaluate_policy(cards, links, events, "score", policy)["load"]
    assert load["cards_per_working_cutoff_mean"] == 2.0
    assert load["cards_per_day_off_cutoff_mean"] == 1.0
    assert load["days_off"].startswith("weekends only")
    # A 06:00 cutoff belongs to its own calendar day.
    six = cards.assign(issued_at=cards.issued_at + pd.Timedelta(hours=6))
    assert select_policy(six, "score", policy).sum() == 12


def _members(rows):
    return pd.DataFrame(
        rows, columns=["event_id", "object_id", "sensor_type", "start_at", "qualifying"]
    ).assign(start_at=lambda f: pd.to_datetime(f.start_at))


def test_static_list_counts_only_events_known_before_the_cutoff():
    t = pd.Timestamp("2024-03-10")
    members = _members(
        [
            ("old", 1, SMOKE, t - pd.Timedelta(days=400), True),  # outside 365 days
            ("a", 1, SMOKE, t - pd.Timedelta(days=100), True),
            ("b", 1, SMOKE, t - pd.Timedelta(days=5), False),  # never qualifies (<= 2 s)
            ("c", 1, SMOKE, t - pd.Timedelta(seconds=1), True),  # qualifies only after t
            ("d", 1, SMOKE, t - pd.Timedelta(hours=1), False),  # qualifies via channel of GAS
            ("d", 1, GAS, t - pd.Timedelta(hours=2), True),
            ("e", 1, GAS, t - pd.Timedelta(hours=3), True),  # GAS member only
            ("e", 1, SMOKE, t + pd.Timedelta(hours=1), False),  # SMOKE joins after t
            ("f", 2, SMOKE, t - pd.Timedelta(days=1), True),
        ]
    )
    cards = pd.DataFrame(
        [
            _card(1, SMOKE, t),
            _card(1, GAS, t),
            _card(2, SMOKE, t),
            _card(3, SMOKE, t),
            _card(1, SMOKE, t + pd.Timedelta(days=1)),
        ]
    )
    gt2s = {"op": ">", "seconds": 2}
    scores = static_list_scores(cards, members, gt2s)
    assert scores.tolist() == [2.0, 2.0, 1.0, 0.0, 4.0]
    # Without a duration filter a qualifying event counts from its first start.
    assert static_list_scores(cards, members, None).tolist() == [3.0, 2.0, 1.0, 0.0, 4.0]
    # Exactly at the filter boundary: known 2 s after the start, visible strictly after.
    edge = _members([("x", 1, SMOKE, t - pd.Timedelta(seconds=2), True)])
    assert static_list_scores(cards.iloc[:1], edge, gt2s).tolist() == [0.0]
    later = _members([("x", 1, SMOKE, t - pd.Timedelta(seconds=3), True)])
    assert static_list_scores(cards.iloc[:1], later, gt2s).tolist() == [1.0]
    assert static_list_scores(cards, members, gt2s, window_days=7).tolist() == [
        1.0,
        2.0,
        1.0,
        0.0,
        3.0,
    ]


def test_event_day_flags_and_slices():
    base = pd.Timestamp("2024-03-04")
    events = pd.DataFrame(
        {
            "event_id": ["n", "m", "d", "x", "y", "z"],
            "object_id": [1, 1, 1, 1, 2, 2],
            "event_start": [
                base + pd.Timedelta(hours=3),  # night
                base + pd.Timedelta(hours=10),  # after a night event of the object
                base + pd.Timedelta(hours=11),  # third of the day
                base + pd.Timedelta(days=1, hours=9),  # alone next day
                base + pd.Timedelta(hours=7),
                base + pd.Timedelta(hours=8),  # after 06:00 only
            ],
            "size": [1] * 6,
            "sensor_types": [[SMOKE]] * 6,
            "system_types": [["Пожарная охрана"]] * 6,
            "qualifies": [True, True, True, True, True, False],
        }
    )
    flags = event_day_flags(events, cutoff_hour=6).set_index("event_id")
    assert flags.prior_same_day_object_event.to_dict() == {
        "n": False,
        "m": True,
        "d": True,
        "x": False,
        "y": False,
        "z": True,
    }
    assert flags.starts_after_cutoff_hour.to_dict() == {
        "n": False,
        "m": True,
        "d": True,
        "x": True,
        "y": True,
        "z": True,
    }
    assert flags.prior_object_event_before_cutoff.to_dict() == {
        "n": False,
        "m": True,
        "d": True,
        "x": False,
        "y": False,
        "z": False,
    }
    cards = pd.DataFrame([_card(1, SMOKE, base, "positive", score=1.0)])
    links = pd.DataFrame(
        {
            "object_id": [1, 1],
            "sensor_type": [SMOKE, SMOKE],
            "issued_at": [base, base],
            "event_id": ["n", "m"],
            "candidate_member": [True, True],
        }
    )
    result = evaluate_policy(cards, links, flags.reset_index(), "score", {"type": "topk", "k": 1})
    assert result["events_prior_same_day_object_event"]["events"] == 1
    assert result["events_not_prior_same_day_object_event"]["events"] == 1


def test_bootstrap_variant_deltas_pairs_weeks_across_card_tables():
    rng = np.random.default_rng(5)
    cards, links, events = [], [], []
    for d in pd.date_range("2024-03-04", periods=28):
        for obj in range(6):
            good = float(rng.random())
            outcome = "positive" if rng.random() < 0.05 + 0.6 * good else "negative"
            cards.append(_card(obj, SMOKE, d, outcome, good=good, noise=float(rng.random())))
            if outcome == "positive":
                eid = f"{obj}-{d.date()}"
                events.append(
                    {
                        "event_id": eid,
                        "object_id": obj,
                        "event_start": d + pd.Timedelta(hours=10),
                        "size": 1,
                        "sensor_types": [SMOKE],
                        "system_types": ["Пожарная охрана"],
                        "qualifies": True,
                    }
                )
                links.append(
                    {
                        "object_id": obj,
                        "sensor_type": SMOKE,
                        "issued_at": d,
                        "event_id": eid,
                        "candidate_member": True,
                    }
                )
    cards, links, events = pd.DataFrame(cards), pd.DataFrame(links), pd.DataFrame(events)
    # The same cards issued six hours later form a separate card table.
    later = cards.assign(issued_at=cards.issued_at + pd.Timedelta(hours=6))
    later_links = links.assign(issued_at=links.issued_at + pd.Timedelta(hours=6))
    top2 = {"type": "topk", "k": 2}
    same = {"cards": cards, "links": links, "events": events, "policy": top2}
    variants = {
        "good": {**same, "score": "good"},
        "noise": {**same, "score": "noise"},
        "good_0600": {**same, "cards": later, "links": later_links, "score": "good"},
    }
    deltas = [("good", "noise", "signal"), ("good_0600", "good", "shift")]
    out = bootstrap_variant_deltas(variants, deltas, replicates=80, seed=17)
    assert out == bootstrap_variant_deltas(variants, deltas, replicates=80, seed=17)
    json.dumps(out)
    assert out["blocks"] == 4
    point = evaluate_policy(cards, links, events, "good", top2)
    assert out["variants"]["good"]["card_precision"]["point"] == pytest.approx(
        point["card_precision"]
    )
    assert out["variants"]["good"]["event_recall"]["point"] == pytest.approx(
        point["events"]["recall"]
    )
    shift = out["deltas"]["shift"]
    assert shift["card_precision"]["point"] == 0.0
    assert shift["card_precision"]["low"] == shift["card_precision"]["high"] == 0.0
    assert out["deltas"]["signal"]["card_precision"]["point"] > 0
    paired = bootstrap_intervals(
        cards,
        links,
        events,
        ["good", "noise"],
        budget=2,
        replicates=80,
        seed=17,
        deltas=[("good", "noise", "signal")],
        schemes=("iso_week",),
    )
    assert paired["schemes"]["iso_week"]["deltas"]["signal"]["card_precision"][
        "point"
    ] == pytest.approx(out["deltas"]["signal"]["card_precision"]["point"])
    with pytest.raises(ValueError, match="delta variants"):
        bootstrap_variant_deltas(variants, [("good", "missing", "x")], replicates=5)


def test_bootstrap_with_policy():
    cards, links, events = _event_setup()
    cards = cards[cards.outcome != "excluded"].reset_index(drop=True)
    cards["other"] = [0.1, 0.9, 0.5]
    out = bootstrap_intervals(
        cards,
        links,
        events,
        ["score", "other"],
        policy={"type": "rolling", "max_open": 1, "window_days": 3},
        replicates=20,
        deltas=[("score", "other", "d")],
    )
    assert out["policy"]["type"] == "rolling"
    with pytest.raises(ValueError, match="exactly one"):
        bootstrap_intervals(cards, links, events, ["score"], replicates=5)

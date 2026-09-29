"""v9: one release rule, causal eligibility, three recall definitions (synthetic)."""

import runpy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from infra_pulse_research.modeling import sensor_failure_fe_v7 as fe7
from infra_pulse_research.modeling import sensor_failure_release as rel
from infra_pulse_research.modeling.sensor_failure_nodes import select_rolling_release

ROOT = Path(__file__).resolve().parents[2]
T0 = pd.Timestamp("2024-01-01")
D = 7
A, B = (1, "s"), (2, "s")


def _world(event_starts: dict, scores: dict, days_n: int = 12, candidate=None, reasons=None):
    """Daily cards of pairs A and B with every event linked to all cards whose window
    holds it. ``event_starts``: id -> (pair, start); ``scores``: pair -> score or a
    function of the day."""
    candidate = candidate or {}
    reasons = reasons or {}
    ev = pd.DataFrame(
        [
            {
                "event_id": e,
                "object_id": p[0],
                "event_start": pd.Timestamp(s),
                "size": 1,
                "sensor_types": [["s"]],
                "system_types": [["x"]],
                "qualifies": True,
            }
            for e, (p, s) in event_starts.items()
        ]
    )
    cards, links = [], []
    for pair in (A, B):
        for d in range(days_n):
            t = T0 + pd.Timedelta(days=d)
            window = [
                (e, pd.Timestamp(s))
                for e, (p, s) in event_starts.items()
                if p == pair and t <= pd.Timestamp(s) < t + pd.Timedelta(days=D)
            ]
            cand = [(e, s) for e, s in window if candidate.get(e, True)]
            for e, s in window:
                links.append(
                    {
                        "object_id": pair[0],
                        "sensor_type": pair[1],
                        "issued_at": t,
                        "event_id": e,
                        "event_start": s,
                        "candidate_member": candidate.get(e, True),
                    }
                )
            score = scores[pair]
            reason = reasons.get((pair, d))
            cards.append(
                {
                    "object_id": pair[0],
                    "sensor_type": pair[1],
                    "system_type": "x",
                    "issued_at": t,
                    "exclusion_reason": reason,
                    "candidate_channels": 0.0 if reason == "no_candidate_channels" else 1.0,
                    "outcome": ("positive" if cand else "negative")
                    if reason is None
                    else "excluded",
                    "y_card": (1.0 if cand else 0.0) if reason is None else np.nan,
                    "first_event_start": min(s for _, s in cand) if cand else pd.NaT,
                    "score": score(d) if callable(score) else score,
                }
            )
    cols = ["object_id", "sensor_type", "issued_at", "event_id", "event_start", "candidate_member"]
    return pd.DataFrame(cards), pd.DataFrame(links, columns=cols), ev


def _issued(cards, mask):
    return [
        (int(o), int((t - T0).days))
        for o, t in cards.loc[mask, ["object_id", "issued_at"]].itertuples(index=False)
    ]


def test_release_is_the_first_event_start_and_the_event_itself_is_caught():
    issued = pd.Series([T0, T0])
    first = pd.Series([T0 + pd.Timedelta(hours=34), T0 + pd.Timedelta(days=9)])
    got = rel.release_at(issued, first, D)
    # First card: released at its event; second: event outside the window -> t + D.
    assert got.tolist() == [first[0], T0 + pd.Timedelta(days=D)]
    assert rel.release_at(issued, first, D, "keep").tolist() == [T0 + pd.Timedelta(days=D)] * 2
    at = [first[0], first[0] + pd.Timedelta(seconds=1)]
    assert rel.is_open(at, [T0, T0], [first[0]] * 2, D).tolist() == [True, False]
    with pytest.raises(ValueError):
        rel.release_at(issued, first, D, "later")


def test_same_day_event_after_the_release_is_not_caught_and_reissue_catches_the_next_day():
    # A: E1 day 2 10:00, E2 day 2 20:00 (same day, after the release), E3 day 3 08:00
    # (22 h after E1: same incident), E4 day 9 12:00 (new incident).
    events = {
        "E1": (A, "2024-01-03 10:00"),
        "E2": (A, "2024-01-03 20:00"),
        "E3": (A, "2024-01-04 08:00"),
        "E4": (A, "2024-01-10 12:00"),
    }
    cards, links, ev = _world(events, {A: 10.0, B: 1.0}, days_n=10)
    first = rel.first_event_starts(cards, links, ev)
    mask = rel.select_rolling(cards, "score", k=1, days=D, first=first)
    # A issued day 0, released at E1 (day 2 10:00); re-issued at the next cutoff (day 3),
    # released at E3 (day 3 08:00); re-issued day 4, released at E4 (day 9 12:00).
    assert _issued(cards, mask) == [(1, 0), (1, 3), (1, 4)]
    h = rel.honest_events(cards, links, ev, mask, D, first=first).set_index("event_id")
    assert h["captured_open"].to_dict() == {"E1": True, "E2": False, "E3": True, "E4": True}
    assert h["captured_first"].to_dict() == {"E1": True, "E2": False, "E3": True, "E4": True}
    assert h.loc["E3", "lead_open"] == pytest.approx(8.0)
    assert not h["repeat"].any()
    inc = rel.incidents(rel.event_pairs(_members(events), ev))
    s = rel.recall_summary(h.reset_index(), inc)
    assert (s["events"], s["incidents"]) == (4, 2)
    assert s["R_repeats"] == s["R_strict"] == pytest.approx(0.75)
    assert s["R_incident"] == pytest.approx(1.0)
    assert s["continuation_share"] == pytest.approx(0.5)
    # E4 starts 6 days after E3 on the same pair: a chronic incident (14-day lookback).
    assert (s["incidents_fresh"], s["incidents_chronic"]) == (1, 1)
    assert s["R_incident_fresh"] == s["R_incident_chronic"] == 1.0
    assert rel.card_summary(cards, mask)["pairs_issued"] == 1
    # Precision does not depend on the recall: every issued A card had an event.
    assert rel.card_summary(cards, mask)["card_precision"] == 1.0


def test_keep_counts_repeats_but_not_strict_and_incident_needs_the_first_event():
    events = {
        "E1": (A, "2024-01-03 10:00"),
        "E2": (A, "2024-01-03 20:00"),
        "E3": (A, "2024-01-05 08:00"),
        "F1": (B, "2024-01-02 12:00"),
        "F2": (B, "2024-01-03 06:00"),
    }
    cards, links, ev = _world(events, {A: 10.0, B: lambda d: 20.0 if d >= 2 else 0.5})
    first = rel.first_event_starts(cards, links, ev)
    keep = rel.select_rolling(cards, "score", k=1, days=D, policy="keep", first=first)
    h = rel.honest_events(cards, links, ev, keep, D, "keep", first).set_index("event_id")
    # K = 1: A is issued on day 0 and kept 7 days; B is never issued while A is open.
    assert _issued(cards, keep)[0] == (1, 0)
    assert h["captured_open"].to_dict() == {
        "E1": True,
        "E2": True,
        "E3": True,
        "F1": False,
        "F2": False,
    }
    assert h["captured_first"].to_dict() == {
        "E1": True,
        "E2": False,
        "E3": False,
        "F1": False,
        "F2": False,
    }
    inc = rel.incidents(rel.event_pairs(_members(events), ev))
    s = rel.recall_summary(h.reset_index(), inc)
    # Incidents: {E1, E2}, {E3}, {F1, F2}; only {E1, E2} and {E3} caught.
    assert s["incidents"] == 3
    assert s["R_repeats"] == pytest.approx(3 / 5) and s["R_strict"] == pytest.approx(1 / 5)
    assert s["R_incident"] == pytest.approx(2 / 3)
    # Release, K = 2: B issued on day 0 (score 0.5 < A but a place is free), caught F1;
    # F2 starts 18 h after F1 -> the same incident; B is re-issued on day 2 (00:00) and
    # catches F2 as its first event: counted by R_strict, not as a new incident.
    mask = rel.select_rolling(cards, "score", k=2, days=D, first=first)
    h2 = rel.honest_events(cards, links, ev, mask, D, first=first)
    s2 = rel.recall_summary(h2, inc)
    assert h2.set_index("event_id").loc[["F1", "F2"], "captured_first"].all()
    assert s2["R_strict"] == pytest.approx(4 / 5)
    assert s2["R_incident"] == pytest.approx(3 / 3)


def test_incident_head_must_be_caught_and_chains_join_through_a_shared_event():
    pairs = pd.DataFrame(
        {
            "event_id": ["a", "b", "c", "d", "e"],
            "object_id": 1,
            "sensor_type": ["p", "p", "q", "q", "p"],
            "event_start": pd.to_datetime(
                [
                    "2024-01-01 00:00",
                    "2024-01-01 23:00",  # p: 23 h after a -> chained
                    "2024-01-02 20:00",  # q: 21 h after b? no (other pair) -> own chain
                    "2024-01-03 10:00",  # q: 14 h after c -> chained with c
                    "2024-01-05 00:00",  # p: 49 h after b -> new incident
                ]
            ),
        }
    )
    # The event c also belongs to pair p (a mixed event): chains {a, b} and {c, d} join
    # only if c is within 24 h of b on p -> 21 h -> joined.
    mixed = pd.concat(
        [pairs, pairs.loc[pairs.event_id == "c"].assign(sensor_type="p")], ignore_index=True
    )
    inc = rel.incidents(pairs).set_index("event_id")
    assert inc.loc["a", "incident"] == inc.loc["b", "incident"] != inc.loc["c", "incident"]
    assert inc.loc["c", "incident"] == inc.loc["d", "incident"] != inc.loc["e", "incident"]
    joined = rel.incidents(mixed).set_index("event_id")
    assert joined.loc[["a", "b", "c", "d"], "incident"].nunique() == 1
    assert joined["incident_first"].to_dict() == {
        "a": True,
        "b": False,
        "c": False,
        "d": False,
        "e": True,
    }
    # A caught continuation does not catch the incident; the gap is strict (< 24 h).
    h = pd.DataFrame(
        {
            "event_id": ["a", "b", "c", "d", "e"],
            "captured_open": [False, True, True, True, True],
            "captured_first": [False, True, True, True, True],
        }
    )
    s = rel.recall_summary(h, joined.reset_index())
    assert (s["incidents"], s["R_incident"]) == (2, 0.5)
    # Fresh: no event of the head's pairs in the 14 days before; e is 73 h after b on p.
    assert joined.set_index("incident_start", append=True)["incident_fresh"].droplevel(
        1
    ).to_dict() == {
        "a": True,
        "b": True,
        "c": True,
        "d": True,
        "e": False,
    }
    exact = pairs.iloc[[0]].assign(event_id="z", event_start=pd.Timestamp("2024-01-02 00:00"))
    two = rel.incidents(pd.concat([pairs.iloc[[0]], exact], ignore_index=True))
    assert two["incident"].nunique() == 2


def test_event_exactly_at_a_cutoff_keeps_the_card_and_is_not_counted_twice():
    events = {"E1": (A, "2024-01-03 00:00")}
    cards, links, ev = _world(events, {A: 10.0, B: 1.0}, days_n=5)
    first = rel.first_event_starts(cards, links, ev)
    mask = rel.select_rolling(cards, "score", k=1, days=D, first=first)
    # The day-0 card is open at 01-03 00:00 (its event), so A is not re-issued that day;
    # the new card of day 3 does not hold E1 (its window starts after it).
    assert _issued(cards, mask) == [(1, 0), (1, 3)]
    h = rel.honest_events(cards, links, ev, mask, D, first=first)
    assert h["captured_first"].tolist() == [True]
    assert rel.card_summary(cards, mask)["card_precision"] == pytest.approx(0.5)


def test_selection_equals_the_previous_next_midnight_rule_off_midnight():
    rng = np.random.default_rng(5)
    starts = {}
    for i in range(25):
        pair = A if rng.random() < 0.6 else B
        day = int(rng.integers(0, 30))
        starts[f"e{i}"] = (
            pair,
            str(T0 + pd.Timedelta(days=day, minutes=int(rng.integers(1, 1439)))),
        )
    cards, links, ev = _world(starts, {A: lambda d: float(d % 5), B: 2.5}, days_n=30)
    first = rel.first_event_starts(cards, links, ev)
    new = rel.select_rolling(cards, "score", k=1, days=D, first=first)
    # The earlier rule: the place is free from the next midnight after the event.
    old_first = first.dt.floor("D") + pd.Timedelta(days=1)
    old = rel.select_rolling(
        cards, "score", k=1, days=D, first=old_first - pd.Timedelta(microseconds=1)
    )
    assert (new == old).all()
    # nodes.select_rolling_release is the same selection.
    via_nodes = select_rolling_release(
        cards, "score", {"type": "rolling_release", "max_open": 1, "window_days": D}
    )
    assert (via_nodes == new).all()


def test_causal_eligibility_uses_only_the_past():
    events = {"E1": (A, "2024-01-03 10:00")}
    reasons = {
        (A, 0): "source_coverage",  # future-dependent: eligible
        (A, 1): "lookback_coverage",  # past: not eligible
        (A, 2): "no_candidate_channels",
        (B, 2): "source_coverage",  # future reason, but its lookback day (day 1) is blocked
        (B, 3): "split_boundary",  # beyond the end: excluded cutoff
    }
    cards, _, _ = _world(events, {A: 1.0, B: 1.0}, days_n=4, reasons=reasons)
    day1 = (cards.object_id == 2) & (cards.issued_at == T0 + pd.Timedelta(days=1))
    cards.loc[day1, "candidate_channels"] = 0.0
    blocked = pd.DataFrame({"day": [T0 + pd.Timedelta(days=1)]})
    ok, beyond = rel.eligibility(cards, D, blocked_days=blocked)
    keys = zip(cards.object_id, (cards.issued_at - T0).dt.days, strict=True)
    got = dict(zip(keys, ok, strict=True))
    assert got == {
        (1, 0): True,
        (1, 1): False,
        (1, 2): False,
        (1, 3): True,
        (2, 0): True,
        (2, 1): False,  # no stored reason but no candidate channel
        (2, 2): False,
        (2, 3): False,
    }
    assert beyond.sum() == 1
    ok_old, _ = rel.eligibility(cards, D, causal=False)
    assert not ok_old[(cards.object_id == 1).to_numpy() & (cards.issued_at == T0).to_numpy()][0]
    _, beyond_end = rel.eligibility(cards, D, period_end=T0 + pd.Timedelta(days=9))
    assert beyond_end.sum() == 2  # day 3 of both pairs crosses the period end
    scoped = pd.DataFrame({"day": [T0 + pd.Timedelta(days=1)], "sensor_type_scope": ["other"]})
    assert (rel.eligibility(cards, D, blocked_days=scoped)[0] == rel.eligibility(cards, D)[0]).all()


def test_unknown_outcome_cards_are_released_by_their_observed_event_and_stay_out_of_p():
    events = {"E1": (A, "2024-01-01 10:00")}
    reasons = {(A, d): "source_coverage" for d in range(4)}
    cards, links, ev = _world(events, {A: 5.0, B: 1.0}, days_n=4, reasons=reasons)
    first = rel.first_event_starts(cards, links, ev)
    mask = rel.select_rolling(cards, "score", k=1, days=D, first=first)
    assert _issued(cards, mask) == [(1, 0), (1, 1)]
    s = rel.card_summary(cards, mask)
    assert (s["cards_known"], s["cards_unknown_outcome"], s["card_precision"]) == (0, 2, None)
    assert s["card_precision_lower_bound"] == 0.0


def test_group_s_last_episode_does_not_depend_on_member_order():
    t = pd.Timestamp("2024-03-10")
    start = pd.Timestamp("2024-03-01 10:00")
    members = pd.DataFrame(
        {
            "event_id": ["e1", "e1", "e1"],
            "episode_id": ["x1", "x2", "x3"],
            "channel_id": [11, 12, 13],
            "object_id": 1,
            "sensor_type": "s",
            "system_type": "x",
            "start_at": [start, start, start],
            "end_at": [start + pd.Timedelta(hours=h) for h in (1, 5, 2)],
            "duration_hours": [1.0, 5.0, 2.0],
            "qualifying": True,
        }
    )
    cards = pd.DataFrame({"object_id": [1], "sensor_type": ["s"], "issued_at": [t]})
    empty = pd.DataFrame()

    def group_s(frame):
        pf = fe7.PairFeatures(
            cards,
            frame,
            empty,
            rule=None,
            pair_days=empty,
            object_days=empty,
            signals=empty,
            holiday_dates=set(),
        )
        return pf.group_s()

    base = group_s(members)
    for seed in range(4):
        shuffled = members.sample(frac=1, random_state=seed).reset_index(drop=True)
        pd.testing.assert_frame_equal(group_s(shuffled), base)
    # The last of equal starts is the highest channel id.
    assert base.loc[0, "p7s__last_duration_h"] == pytest.approx(2.0)


V9 = runpy.run_path(str(ROOT / "data-science/scripts/final_sensor_failure_v9.py"), run_name="v9")


def _point(days, k, p, r_inc, r_strict=None, scope="phase", score="static_list"):
    r_strict = r_inc if r_strict is None else r_strict
    folds = [
        {"card_precision": p, "R_repeats": r_strict, "R_strict": r_strict, "R_incident": r_inc}
    ] * 2
    return {"scope": scope, "days": days, "score": score, "k": k, **V9["summarize"](folds)}


def test_prod_rule_takes_the_smallest_k_with_stable_neighbours():
    points = [
        _point(14, 3, 0.80, 0.45),
        _point(14, 4, 0.78, 0.52),  # passes, but its neighbour K = 5 has P 0.70
        _point(14, 5, 0.70, 0.60),
        _point(14, 6, 0.76, 0.55),  # neighbours 5 (0.70) -> fails
        _point(7, 6, 0.75, 0.51),
        _point(7, 5, 0.74, 0.40),
        _point(7, 7, 0.73, 0.55),  # K = 6 at 7 days: neighbours 0.74 and 0.73 -> passes
        _point(21, 2, 0.95, 0.90),  # horizon outside the rule
        _point(14, 2, 0.99, 0.99, score="decay_90"),  # score outside the rule
    ]
    got = V9["choose_prod"](points, "R_incident")
    assert got["goal_met"] and (got["point"]["days"], got["point"]["k"]) == (7, 6)
    # With R_strict below 0.5 everywhere the fallback is the best min F1 at 14 days.
    strict = [dict(p, **{"min_R_strict": 0.3}) for p in points]
    for p in strict:
        p["min_f1_R_strict"] = 2 * p["min_precision"] * 0.3 / (p["min_precision"] + 0.3)
    fb = V9["choose_prod"](strict, "R_strict")
    assert not fb["goal_met"] and fb["point"]["days"] == 14 and fb["point"]["k"] == 3
    assert "не выполнена" in fb["note"]


def test_demo_rule_takes_the_best_worse_fold_f1_at_14_days():
    points = [
        _point(14, 3, 0.6, 0.3, scope="S1"),
        _point(14, 8, 0.5, 0.5, scope="S1"),
        _point(21, 8, 0.9, 0.9, scope="S1"),
        _point(14, 3, 0.4, 0.4, scope="S4"),
        _point(14, 5, 0.6, 0.6, scope="fire"),
    ]
    got = V9["choose_demo"](points, "R_incident")
    assert (got["S1"]["k"], got["S4"]["k"], got["fire"]["k"]) == (8, 3, 5)


def test_scopes_and_budgets():
    frame = pd.DataFrame(
        {
            "sensor_type": [
                "Состояние фазы",
                "Датчик затопления",
                "Датчик дыма",
                "ИБП",
                "КД Дверь",
            ],
            "system_type": ["Диспетчерский контроль"] * 2 + ["Пожарная охрана", "x", "y"],
        }
    )
    assert V9["scope_mask"](frame, "phase").tolist() == [True, False, False, False, False]
    assert V9["scope_mask"](frame, "S3").tolist() == [True, True, False, False, False]
    assert V9["scope_mask"](frame, "S1").tolist() == [True, False, True, False, True]
    assert V9["scope_mask"](frame, "S4").tolist() == [False, False, True, False, True]
    assert V9["scope_mask"](frame, "fire").tolist() == [False, False, True, False, False]
    assert V9["budgets"]("phase", 7) == list(range(1, 16))
    assert V9["budgets"]("S1", 14) == list(range(8, 15))
    assert V9["budgets"]("S4", 7) == [3, 5, 7]
    assert V9["budgets"]("fire", 21) == list(range(3, 22))
    # Masked labels use their base scope; the fire-detector target gets 1..D.
    assert V9["scope_mask"](frame, "S4+mask").tolist() == V9["scope_mask"](frame, "S4").tolist()
    assert V9["budgets"]("S1+mask", 14) == list(range(8, 15))
    assert V9["budgets"]("fire_detector", 14) == list(range(1, 15))


def _members(events: dict) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"event_id": e, "object_id": p[0], "sensor_type": p[1], "start_at": pd.Timestamp(s)}
            for e, (p, s) in events.items()
        ]
    )


def test_report_writer_keeps_json_valid_and_rejects_identifiers(tmp_path):
    point = {
        "scope": "phase",
        "days": 14,
        "score": "static_list",
        "k": 6,
        "folds": [{"card_precision": 0.73456789, "R_incident": 0.5}, {"card_precision": None}],
    }
    row = V9["compact"](point)
    assert row[:4] == ["phase", 14, "static_list", 6] and row[4][0][0] == 0.7346
    path = tmp_path / "r.json"
    V9["write_json"](path, {"a": 1, "points": [row, row]}, one_line="points")
    import json

    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1, "points": [row, row]}
    with pytest.raises(ValueError):
        V9["write_json"](path, {"object_id": 1})


def test_frozen_plan_holds_the_frozen_configurations():
    plan = {name: (role, label, k) for name, role, label, k, _ in V9["FROZEN_PLAN"]}
    assert plan["prod_phase"] == ("primary", "phase", 10)
    assert plan["prod_phase_k7"] == ("secondary", "phase", 7)
    assert [n for n, (r, *_) in plan.items() if r == "primary"] == ["prod_phase"]
    masked = sorted((lab, k) for _, lab, k in plan.values() if lab.endswith("+mask"))
    assert masked == [
        ("S1+mask", 13),
        ("S4+mask", 5),
        ("S4+mask", 14),
        ("fire+mask", 5),
        ("fire+mask", 6),
    ]
    # Every masked demo point has the same point without the mask beside it.
    plain = {(lab, k) for _, lab, k in plan.values()}
    assert all((lab.removesuffix("+mask"), k) in plain for lab, k in masked)
    assert plan["demo_fire_detector_failure_k5"] == ("demo", "fire_detector", 5)
    assert V9["PERIODS"]["holdout"]["start"] == "2025-07-01" and V9["HOLDOUT_USE"] == 5


def test_freeze_log_allows_one_run_and_one_logged_repeat():
    code = {"a.py": "1", "b.py": "2"}
    freeze = {"event": "freeze", "sha256": {"config": "c", **code}}
    check = V9["check_freeze"]
    assert check([], "c", code, False) == "no freeze entry"
    assert check([freeze], "c", code, False) == ""
    assert "config differs" in check([freeze], "x", code, False)
    assert "code differs" in check([freeze], "c", {**code, "b.py": "3"}, False)
    started = {"event": "holdout_started"}
    assert "only --repeat" in check([freeze, started], "c", code, False)
    assert "repeat_after_fix" in check([freeze, started], "c", code, True)
    fixed = {"event": "repeat_after_fix", "sha256": {"config": "c", "a.py": "1", "b.py": "3"}}
    log = [freeze, started, fixed]
    assert check(log, "c", {**code, "b.py": "3"}, True) == ""
    assert "code differs" in check(log, "c", code, True)
    done = [*log, {"event": "holdout_started"}, {"event": "holdout_finished"}]
    assert "finished" in check(done, "c", {**code, "b.py": "3"}, True)


def _frames(y, caught, fresh, strict):
    at = pd.date_range("2025-07-07", periods=len(y), freq="7D")
    heads_at = pd.date_range("2025-07-07", periods=len(caught), freq="7D")
    return {
        "cards": pd.DataFrame({"at": at, "object_id": np.arange(len(y)) % 3, "y": y}),
        "events": pd.DataFrame(
            {
                "at": heads_at,
                "object_id": np.arange(len(caught)) % 3,
                "strict": strict,
                "repeats": strict,
            }
        ),
        "heads": pd.DataFrame(
            {
                "at": heads_at,
                "object_id": np.arange(len(caught)) % 3,
                "caught": caught,
                "fresh": fresh,
            }
        ),
    }


def test_block_bootstrap_points_equal_the_rates_and_draws_are_paired():
    a = _frames(
        np.array([1.0, 0.0, 1.0, 1.0]),
        np.array([1.0, 0.0, 1.0]),
        np.array([True, True, False]),
        np.array([1.0, 0.0, 0.0]),
    )
    b = _frames(
        np.array([1.0, 1.0, 1.0, 1.0]),
        np.array([1.0, 1.0, 1.0]),
        np.array([True, False, False]),
        np.array([1.0, 1.0, 1.0]),
    )
    out = V9["block_bootstrap"]({"a": a, "b": b}, "iso_week", replicates=50)
    m = out["metrics"]
    assert m["a"]["card_precision"]["point"] == pytest.approx(0.75)
    assert m["a"]["R_incident"]["point"] == pytest.approx(2 / 3)
    assert m["a"]["R_incident_fresh"]["point"] == pytest.approx(0.5)
    assert m["a"]["R_incident_chronic"]["point"] == pytest.approx(1.0)
    assert m["b"]["card_precision"]["low"] == m["b"]["card_precision"]["high"] == 1.0
    assert out == V9["block_bootstrap"]({"a": a, "b": b}, "iso_week", replicates=50)
    objects = V9["block_bootstrap"]({"a": a}, "object_id", replicates=50)
    assert objects["blocks"] == 3 and objects["seed"] == V9["SEED"]


def test_verdict_uses_the_lower_bounds_and_the_point_separately():
    week = {"card_precision": {"low": 0.66}, "R_incident": {"low": 0.55}}
    v = V9["verdict"]({"card_precision": 0.74, "R_incident": 0.62}, week)
    assert v["point"]["pass"] and not v["lower_bound_weeks"]["pass"]
    week_ok = {"card_precision": {"low": 0.71}, "R_incident": {"low": 0.50}}
    assert V9["verdict"]({"card_precision": 0.8, "R_incident": 0.6}, week_ok)["lower_bound_weeks"][
        "pass"
    ]


POSTHOC = runpy.run_path(
    str(ROOT / "data-science/scripts/posthoc_sensor_failure_v9_baselines.py"), run_name="ph"
)


def test_posthoc_channel_classes_by_name():
    cls = POSTHOC["channel_class"]
    assert cls("АВР Ввод1 ПК2 Э/щ") == "input" and cls("ЩАП-1 Ввод2 ПК3") == "input"
    assert cls("Межсекционный АВ ПК4") == "input" and cls("Ввод2 ПК5 щит.ЩАП-1") == "input"
    for name in ("ГРО3 ПК5", "Группа ГРО1 ПК2-3", "Фидер ФВ1 (В2)", "ФАНС2 ПК4", "РО1 ПК3"):
        assert cls(name) == "feeder", name
    assert cls("Ф.Резерв 2") == "feeder" and cls("Питание ПУИ 3") == "pui_supply"
    assert cls("ОК-1 вкл") == "other"
    assert POSTHOC["input_kind"]("АВР Ввод1") == "АВР"


def test_posthoc_persistence_uses_only_recent_past_events():
    members = pd.DataFrame(
        {
            "event_id": ["a", "b"],
            "object_id": [1, 2],
            "sensor_type": "s",
            "start_at": pd.to_datetime(["2024-01-01 10:00", "2023-12-01 10:00"]),
            "qualifying": True,
        }
    )
    t = pd.Timestamp("2024-01-03")
    cards = pd.DataFrame(
        {
            "object_id": [1, 1, 2],
            "sensor_type": "s",
            "issued_at": [pd.Timestamp("2024-01-01"), t, t],
        }
    )
    got = POSTHOC["persistence_scores"](cards, members)
    # 01-01 00:00: the event of 10:00 is in the future; 01-03: 38 h ago; pair 2: > 14 days.
    assert np.isnan(got[0]) and got[1] == pytest.approx(-38.0) and np.isnan(got[2])


def test_posthoc_fast_evaluator_equals_the_v9_point():
    events = {
        "E1": (A, "2024-01-03 10:00"),
        "E2": (A, "2024-01-03 20:00"),
        "E3": (A, "2024-01-06 08:00"),
        "F1": (B, "2024-01-02 12:00"),
        "F2": (B, "2024-01-09 06:00"),
    }
    cards, links, ev_ = _world(events, {A: 10.0, B: lambda d: 20.0 if d >= 2 else 0.5})
    first = rel.first_event_starts(cards, links, ev_)
    inc = rel.incidents(rel.event_pairs(_members(events), ev_))
    fast = POSTHOC["FastEvaluator"](cards, links, ev_, first, inc)
    none = np.zeros(len(cards), dtype=bool)
    for k in (1, 2):
        mask = rel.select_rolling(cards, "score", k=k, days=D, first=first)
        ref = V9["point"](cards, links, ev_, mask, D, first, none, inc, none)
        got = fast.metrics(mask)
        for m in ("card_precision", "R_incident", "R_incident_fresh", "R_strict", "pairs_issued"):
            assert got[m] == pytest.approx(ref[m]), (k, m)


V10 = runpy.run_path(
    str(ROOT / "data-science/scripts/phase_catboost_compact_v10.py"), run_name="v10"
)


def test_v10_features_use_only_the_past_and_stay_compact():
    assert len(V10["FEATURES"]) == 20 and set(V10["CAT_FEATURES"]) <= set(V10["FEATURES"])
    phase = V10["V9"]["PHASE"]
    t = pd.Timestamp("2024-03-10")
    members = pd.DataFrame(
        {
            "event_id": ["a", "b", "c", "d"],
            "object_id": 1,
            "sensor_type": phase,
            "channel_id": [11, 12, 11, 13],
            "start_at": [
                t - pd.Timedelta(days=1, hours=-3),
                t - pd.Timedelta(days=3),
                t,
                t - pd.Timedelta(days=40),
            ],
            "qualifying": True,
        }
    )
    cards = pd.DataFrame({"object_id": [1], "sensor_type": [phase], "issued_at": [t]})
    f = V10["pair_features"](cards, members).iloc[0]
    # The event at exactly t is not seen; yesterday 03:00, three days ago and 40 days ago are.
    assert (f["events_7d"], f["events_30d"], f["events_90d"]) == (2, 2, 3)
    assert (f["lag_1"], f["lag_2"], f["lag_3"]) == (1, 0, 1)
    assert f["days_since_last"] == pytest.approx(21 / 24)
    assert f["day_of_week"] == 6
    kind = V10["feeder_kind"]
    assert kind("ГРО3 ПК5") == "lighting" and kind("Фидер ФВ1 (В2)") == "ventilation"
    assert kind("ФАНС2 ПК4") == "pumps" and kind("ОЗК В1 ПК2 закр.") == "ozk"
    assert kind("ФТС1 ПК2") == "other"
    cards2 = pd.DataFrame(
        {
            "issued_at": pd.to_datetime(["2023-12-10", "2023-12-25", "2024-01-20"]),
            "outcome": "negative",
        }
    )
    assert V10["train_mask"](cards2, "2023", "2024").tolist() == [True, False, False]
    assert V10["train_mask"](cards2, "2024", "2023").tolist() == [False, False, True]

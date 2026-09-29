"""v5 exploration 2: list variants (window, decay, node) are point in time; goal per fold."""

import runpy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from infra_pulse_research.modeling.sensor_failure_evaluation import static_list_scores

ROOT = Path(__file__).resolve().parents[2]
P2 = runpy.run_path(
    str(ROOT / "data-science/scripts/explore_sensor_failure_v5_part2.py"), run_name="t"
)
DAY0 = pd.Timestamp("2024-03-01")


def _members(rows):
    """rows: (event, object, channel, day offset, seconds of the episode)."""
    return pd.DataFrame(
        [
            {
                "event_id": e,
                "object_id": obj,
                "sensor_type": "s",
                "channel_id": ch,
                "start_at": DAY0 + pd.Timedelta(days=d),
                "qualifying": seconds >= 2,
            }
            for e, obj, ch, d, seconds in rows
        ]
    )


def _cards(rows):
    """rows: (object, day offset)."""
    return pd.DataFrame(
        [
            {
                "object_id": obj,
                "sensor_type": "s",
                "issued_at": DAY0 + pd.Timedelta(days=d),
                "month": (DAY0 + pd.Timedelta(days=d)).strftime("%Y-%m"),
            }
            for obj, d in rows
        ]
    )


RULE = {"op": ">=", "seconds": 2}


def test_rebuilt_window_count_equals_the_static_list():
    members = _members(
        [("a", 1, 11, -400, 5), ("b", 1, 11, -100, 5), ("c", 1, 11, -1, 1), ("d", 1, 11, 3, 5)]
    )
    cards = _cards([(1, 0), (1, 5), (2, 5)])
    times = P2["unit_event_times"](members, RULE)
    got = P2["window_counts"](cards, times, 365)
    assert np.array_equal(got, static_list_scores(cards, members, RULE).to_numpy())
    # Only b counts on day 0 (a is older than 365 days, c does not qualify, d is later).
    assert got.tolist() == [1.0, 2.0, 0.0]


def test_decayed_count_uses_only_events_before_the_cutoff():
    members = _members([("a", 1, 11, 0, 5), ("b", 1, 11, 30, 5)])
    cards = _cards([(1, 30), (1, 60)])
    times = P2["unit_event_times"](members, None)
    got = P2["decayed_counts"](cards, times, 30)
    # Day 30: only a (b starts exactly at the cutoff); day 60: a is 60 days old, b 30.
    assert got == pytest.approx([0.5, 0.25 + 0.5])
    later = _members([("a", 1, 11, 0, 5), ("b", 1, 11, 30, 5), ("z", 1, 11, 61, 5)])
    again = P2["decayed_counts"](cards, P2["unit_event_times"](later, None), 30)
    assert again == pytest.approx(got)


def test_node_count_adds_the_events_of_node_peers_only():
    members = _members(
        [("e1", 2, 21, 1, 5), ("e2", 3, 31, 2, 5), ("e3", 1, 11, 3, 5), ("e4", 2, 21, 12, 5)]
    )
    cards = _cards([(1, 10), (2, 10), (3, 10)])
    snap = pd.DataFrame(
        {
            "channel_id": [11, 21, 31],
            "in_node": [True, True, False],
            "node_id": [11, 11, 31],
            "node_size": [2, 2, 1],
        }
    )
    chans = pd.DataFrame(
        {"channel_id": [11, 21, 31], "object_id": [1, 2, 3], "sensor_type": ["s", "s", "s"]}
    )
    got = P2["node_counts"](
        cards,
        members,
        RULE,
        "t",
        snapshot=lambda month: snap,
        channels=lambda target, month: chans,
    )
    # Units 1 and 2 share a node: each sees e1 and e3; unit 3 only e2; e4 is after t.
    assert got.tolist() == [2.0, 2.0, 1.0]
    assert static_list_scores(cards, members, RULE).tolist() == [1.0, 1.0, 1.0]


def test_goal_in_every_fold_requires_each_fold():
    def point(folds):
        return {"folds": [{"card_precision": p, "event_recall": r} for p, r in folds]}

    points = {
        "a": {"10": point([(0.72, 0.51), (0.75, 0.55)]), "12": point([(0.68, 0.52), (0.80, 0.6)])},
        "ceiling": {"10": point([(1.0, 0.9), (1.0, 0.9)])},
    }
    got = P2["goal_in_every_fold"](points)
    assert [(g["score"], g["k"]) for g in got] == [("a", 10)]
    assert got[0]["min_precision"] == pytest.approx(0.72)

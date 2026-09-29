"""v5 exploration 4: honest recall and repeats under the release and keep policies."""

import runpy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
P4 = runpy.run_path(
    str(ROOT / "data-science/scripts/explore_sensor_failure_v5_part4.py"), run_name="t"
)
T = pd.Timestamp("2024-03-04")


def _world():
    first, second = T + pd.Timedelta(hours=10), T + pd.Timedelta(days=3, hours=5)
    cards = pd.DataFrame(
        {
            "object_id": [1, 1],
            "sensor_type": "s",
            "system_type": "x",
            "issued_at": [T, T + pd.Timedelta(days=5)],
            "outcome": ["positive", "negative"],
            "y_card": [1.0, 0.0],
            "exclusion_reason": None,
            "first_event_start": [first, pd.NaT],
            "object_failing_past_24h": False,
            "weekend": False,
        }
    )
    links = pd.DataFrame(
        {
            "object_id": 1,
            "sensor_type": "s",
            "issued_at": [T, T],
            "event_id": ["e1", "e2"],
            "candidate_member": True,
        }
    )
    events = pd.DataFrame(
        {
            "event_id": ["e1", "e2"],
            "object_id": 1,
            "event_start": [first, second],
            "size": 1,
            "sensor_types": [["s"], ["s"]],
            "system_types": [["x"], ["x"]],
            "qualifies": True,
        }
    )
    return cards, links, events


def test_release_counts_only_events_before_the_release_keep_counts_repeats():
    cards, links, events = _world()
    mask = np.array([True, False])
    release = P4["honest_outcomes"](cards, links, events, mask, 7, keep=False).set_index("event_id")
    keep = P4["honest_outcomes"](cards, links, events, mask, 7, keep=True).set_index("event_id")
    # The protocol recall captures both events in both cases.
    assert release["captured"].all() and keep["captured"].all()
    # Release: the card leaves the day after e1, so e2 is not honestly captured.
    assert release["captured_open"].tolist() == [True, False]
    # Keep: the card stays open, e2 is captured and is a repeat of the same card.
    assert keep["captured_open"].tolist() == [True, True]
    assert keep.loc["e1", "repeat"] == False  # noqa: E712
    assert keep.loc["e2", "repeat"] == True  # noqa: E712
    assert keep.loc["e2", "lead_open"] == pytest.approx(77.0)


def test_point_reports_repeat_share_and_summary_uses_the_worse_fold():
    cards, links, events = _world()
    mask = np.array([True, False])
    prev = np.array([True, False])
    m = P4["point"](cards, links, events, mask, 7, True, prev)
    assert m["event_recall_open"] == 1.0 and m["repeat_share_captured"] == 0.5
    assert m["chronic_share_issued"] == 1.0 and m["card_precision"] == 1.0
    other = {**m, "card_precision": 0.6, "event_recall_open": 0.4}
    s = P4["summarize"]([m, other])
    assert s["min_precision"] == 0.6 and s["min_recall_open"] == 0.4
    assert s["gap_worst_fold"] == pytest.approx(np.hypot(0.1, 0.1))
    assert P4["policy"]("keep", 21, 12) == {"type": "rolling", "max_open": 12, "window_days": 21}

"""Point-in-time co-failure nodes, node score propagation and unit N cards (v4, I1)."""

import numpy as np
import pandas as pd
import pytest

from infra_pulse_research.modeling.sensor_failure_evaluation import evaluate_policy
from infra_pulse_research.modeling.sensor_failure_nodes import (
    cofailure_groups,
    node_cards,
    node_map,
    propagate_node_scores,
)

T = pd.Timestamp


def _members(events):
    """events: list of (event_id, start, [channels]); members start a minute apart."""
    rows = []
    for eid, start, channels in events:
        for i, c in enumerate(channels):
            rows.append((eid, c, 10, T(start) + pd.Timedelta(minutes=i)))
    return pd.DataFrame(rows, columns=["event_id", "channel_id", "object_id", "start_at"])


EVENTS = [
    ("a", "2024-01-05 10:00", [1, 2, 3]),
    ("b", "2024-01-20 10:00", [1, 2, 3]),
    ("c", "2024-02-10 10:00", [1, 2]),
    ("d", "2024-02-15 10:00", [1, 2, 3, 4]),
    ("e", "2024-02-16 10:00", [4]),
    ("f", "2024-02-17 10:00", [4]),
    ("g", "2024-02-18 10:00", [4]),
    # 6 and 7 co-fail three times only in March.
    ("h", "2024-03-02 10:00", [6, 7]),
    ("i", "2024-03-09 10:00", [6, 7]),
    ("j", "2024-03-16 10:00", [6, 7]),
]


def test_groups_follow_the_rule_and_use_only_past_starts():
    members = _members(EVENTS)
    march = cofailure_groups(members, "2024-03-01")
    # 1-2: 4 joint of 4; 1-3 and 2-3: 3 of 3; 4 fails mostly alone (1 of 4 with the others).
    assert march == {1: 1, 2: 1, 3: 1}
    april = cofailure_groups(members, "2024-04-01")
    assert april[6] == april[7] == 6 and april[1] == 1 and 4 not in april
    # The March snapshot equals the one built from a journal cut at the snapshot time.
    cut = members[members.start_at < T("2024-03-01")]
    assert cofailure_groups(cut, "2024-03-01") == march
    # Only two joint events before the third one: no group yet.
    assert 6 not in cofailure_groups(members, "2024-03-16 10:00")
    # The prefix of an event straddling the snapshot counts only its earlier members.
    assert cofailure_groups(members, "2024-01-20 10:01") == {}
    # Large events add no pairs, but still count in the single-channel totals:
    # without event d, 1-2 share 3 of 4 events of channel 1.
    assert cofailure_groups(members, "2024-03-01", max_event_size=3) == {}
    assert cofailure_groups(members, "2024-03-01", max_event_size=3, min_ratio=0.7) == {1: 1, 2: 1}


def test_node_map_makes_singletons_their_own_node():
    nodes = node_map([1, 2, 3, 4, 5], {1: 1, 2: 1, 3: 1}).set_index("channel_id")
    assert nodes.loc[3, "node_id"] == 1 and nodes.loc[3, "node_size"] == 3
    assert nodes.loc[4, "node_id"] == 4 and nodes.loc[4, "node_size"] == 1
    assert not nodes.loc[5, "in_node"] and nodes.loc[1, "in_node"]


def test_propagation_gives_channels_their_node_score():
    t = T("2024-03-05")
    channels = pd.DataFrame(
        {"channel_id": [1, 2, 3, 4], "issued_at": [t] * 4, "p": [0.1, 0.5, 0.2, 0.3]}
    )
    nodes = node_map([1, 2, 3, 4], {1: 1, 2: 1, 3: 1})
    assert propagate_node_scores(channels, "p", nodes, "max").tolist() == [0.5, 0.5, 0.5, 0.3]
    noisy = propagate_node_scores(channels, "p", nodes, "noisy_or")
    assert noisy[0] == pytest.approx(1 - 0.9 * 0.5 * 0.8) and noisy[3] == pytest.approx(0.3)
    with pytest.raises(ValueError):
        propagate_node_scores(channels, "p", nodes, "mean")


def test_unit_n_cards_links_and_evaluation():
    t = T("2024-03-05")
    labels = pd.DataFrame(
        {
            "channel_id": [1, 2, 3, 4, 5],
            "object_id": [10, 10, 10, 10, 10],
            "issued_at": [t] * 5,
            "split_period": ["development"] * 5,
            "known": [True] * 5,
            "candidate": [True, False, True, False, True],
            "window_reason_24h": [None] * 5,
        }
    )
    nodes = node_map([1, 2, 3, 4, 5], {1: 1, 2: 1, 3: 1})
    members = pd.DataFrame(
        {
            "event_id": ["x", "x", "y", "z"],
            "channel_id": [2, 3, 4, 5],
            "start_at": [t + pd.Timedelta(hours=3)] * 2
            + [t + pd.Timedelta(hours=5), t - pd.Timedelta(hours=1)],
        }
    )
    events = pd.DataFrame(
        {
            "event_id": ["x", "y", "z"],
            "object_id": [10, 10, 10],
            "event_start": [
                t + pd.Timedelta(hours=3),
                t + pd.Timedelta(hours=5),
                t - pd.Timedelta(hours=1),
            ],
            "size": [2, 1, 1],
            "sensor_types": [["s"]] * 3,
            "system_types": [["x"]] * 3,
            "qualifies": [True, True, True],
        }
    )
    cards, links = node_cards(labels, nodes, members, events)
    by = cards.set_index("node_id")
    assert list(by.index) == [1, 4, 5]
    assert (by.loc[1, "y_card"], by.loc[1, "candidate_channels"]) == (1.0, 2)
    # Node 4 has no candidate channel; node 5's event started before t.
    assert by.loc[4, "exclusion_reason"] == "no_candidate_channels"
    assert (by.loc[5, "outcome"], by.loc[5, "y_card"]) == ("negative", 0.0)
    assert set(links.event_id) == {"x", "y"}
    cards["score"] = [0.9, 0.1, 0.5]
    result = evaluate_policy(cards, links, events, "score", {"type": "topk", "k": 1})
    assert result["card_precision"] == 1.0 and result["events"]["recall"] == pytest.approx(0.5)
    assert np.isnan(cards.loc[cards.node_id == 4, "y_card"]).all()
